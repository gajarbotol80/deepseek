"""
Pure-HTTP DeepSeek chat client.

Speaks chat.deepseek.com's internal API directly using a captured signed-in
session (see `deepseek.auth`). For each message it:

    1. creates a chat session   (POST /api/v0/chat_session/create)
    2. fetches a PoW challenge   (POST /api/v0/chat/create_pow_challenge)
    3. solves it via the WASM    (deepseek.pow.DeepSeekPow)
    4. POSTs the completion       with the x-ds-pow-response header
    5. parses the SSE stream      into text

    from deepseek.auth import get_session
    from deepseek.client import DeepSeekClient

    client = DeepSeekClient(get_session())
    print(client.chat("Hello!"))                 # full reply
    for chunk in client.stream("Tell a joke"):   # streamed
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional

import httpx

from .auth import Session, get_session
from .pow import DeepSeekPow

BASE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"
UPLOAD_PATH = "/api/v0/file/upload_file"
FETCH_FILES_PATH = "/api/v0/file/fetch_files"

# Maps DeepSeek's fragment `type` to our two output channels. "RESPONSE" is
# confirmed (the original working implementation). The reasoning entries are
# an unverified best guess — see the DEEPSEEK_DEBUG_FRAGMENTS note in
# _parse_sse if reasoning text doesn't come through with thinking=True.
_FRAGMENT_KIND = {
    "RESPONSE": "content",
    "THINK": "reasoning",
    "THINKING": "reasoning",
}

# DeepSeek's mode pill, sent as `model_type` in the completion body. "default" is
# Instant (the fast model); "expert" is the stronger, slower model. Omitting the
# field lets the backend pick, so we always send one explicitly.
DEFAULT_MODEL_TYPE = "default"

# A conversation_id is an opaque "<chat_session_id>:<last_message_id>" token. It
# carries everything needed to resume a thread, so the client stays stateless.
_CID_SEP = ":"


def _encode_cid(session_id: str, message_id: Optional[int]) -> str:
    if message_id is None:
        return session_id
    return f"{session_id}{_CID_SEP}{message_id}"


def _decode_cid(conversation_id: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Split a conversation_id back into (chat_session_id, parent_message_id)."""
    if not conversation_id:
        return None, None
    session_id, _, msg = conversation_id.partition(_CID_SEP)
    parent = int(msg) if msg.isdigit() else None
    return (session_id or None), parent


@dataclass
class Reply:
    """A completed chat reply plus the id to resume the conversation."""

    text: str
    conversation_id: str
    reasoning: str = ""  # DeepThink's reasoning trace, when `thinking=True` was set

    def __str__(self) -> str:  # so print(reply) shows the text
        return self.text


def _biz(data: dict) -> dict:
    """Unwrap DeepSeek's `data.biz_data` envelope, raising on API-level errors."""
    if data.get("code") != 0:
        raise RuntimeError(f"DeepSeek API error: {data.get('msg') or data}")
    biz = data.get("data", {}).get("biz_data")
    if biz is None:
        raise RuntimeError(f"Unexpected response shape: {data}")
    return biz


class DeepSeekClient:
    def __init__(
        self,
        session: Optional[Session] = None,
        allow_interactive: bool = True,
    ):
        # `allow_interactive=False` makes session resolution non-blocking: it
        # uses a cached/headless session and raises LoginRequired instead of
        # opening a browser window. The server passes False (see server/api.py).
        self.session = session or get_session(allow_interactive=allow_interactive)
        self._pow = DeepSeekPow()
        # The wasmtime Store behind the PoW solver is not reentrant; serialise
        # access so concurrent server requests don't corrupt it.
        self._pow_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookies,
            timeout=httpx.Timeout(120.0, read=300.0),
        )

    def _base_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.session.token}",
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.session.user_agent,
            "origin": BASE,
            "referer": f"{BASE}/",
            "x-app-version": "2.0.0",
            "x-client-version": "2.0.0",
            "x-client-platform": "web",
            "x-client-locale": "en_US",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-timezone-offset": "19800",
        }

    # --- protocol steps -----------------------------------------------------

    def create_chat_session(self) -> str:
        r = self._http.post("/api/v0/chat_session/create", json={})
        r.raise_for_status()
        return _biz(r.json())["chat_session"]["id"]

    def _pow_header(self, target_path: str = COMPLETION_PATH) -> str:
        r = self._http.post(
            "/api/v0/chat/create_pow_challenge", json={"target_path": target_path}
        )
        r.raise_for_status()
        challenge = _biz(r.json())["challenge"]
        with self._pow_lock:
            return self._pow.make_header(challenge)

    def upload_image(self, data: bytes, filename: str = "image.jpg",
                      content_type: str = "image/jpeg") -> str:
        """Upload an image for a vision request. Returns a `file_id`
        (e.g. "file-<uuid>") — pass it in `ref_file_ids` on the *next*
        `chat`/`stream` call. The file starts in "PENDING" status; call
        `wait_for_file` before referencing it (DeepSeek needs a moment to
        process it, confirmed via a captured real upload+completion pair).

        Uses a short-lived plain httpx.Client rather than `self._http`: the
        shared client defaults to `content-type: application/json`, and once
        that header is set, httpx won't override it with the multipart
        boundary type `files=` needs — a well-known httpx gotcha. A fresh
        client with no default content-type sidesteps it cleanly.
        """
        pow_header = self._pow_header(target_path=UPLOAD_PATH)
        headers = {
            **self._base_headers(),
            "x-ds-pow-response": pow_header,
            "x-file-size": str(len(data)),
            "x-model-type": "vision",
        }
        headers.pop("content-type", None)  # let httpx set the multipart boundary
        with httpx.Client(base_url=BASE, cookies=self.session.cookies,
                           timeout=httpx.Timeout(120.0, read=300.0)) as http:
            r = http.post(UPLOAD_PATH, headers=headers,
                          files={"file": (filename, data, content_type)})
        r.raise_for_status()
        return _biz(r.json())["id"]

    def wait_for_file(self, file_id: str, timeout: float = 60.0,
                       interval: float = 1.0) -> dict:
        """Poll fetch_files until the upload leaves "PENDING" or `timeout`
        seconds pass. Returns the file record on success; raises RuntimeError
        if DeepSeek reports a failure, or TimeoutError if it never finishes."""
        deadline = time.monotonic() + timeout
        while True:
            r = self._http.get(FETCH_FILES_PATH, params={"file_ids": file_id})
            r.raise_for_status()
            files = _biz(r.json()).get("files", [])
            if files and files[0].get("status") != "PENDING":
                rec = files[0]
                if rec.get("status") != "SUCCESS":
                    raise RuntimeError(
                        f"DeepSeek couldn't process the image (status="
                        f"{rec.get('status')!r}, error_code={rec.get('error_code')!r})"
                    )
                return rec
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timed out waiting for {file_id} to finish processing")
            time.sleep(interval)

    # --- public API ---------------------------------------------------------

    def stream(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
        ref_file_ids: Optional[List[str]] = None,
    ) -> "_Stream":
        """Stream a reply. Iterate it for text chunks; read `.conversation_id`
        afterwards to resume the thread. Pass an existing `conversation_id` to
        continue a previous conversation.

        `model` is DeepSeek's model_type wire value: "default" (Instant) or
        "expert"; it defaults to "default" on a NEW thread. It cannot be combined
        with `conversation_id` — a thread's model is fixed when it's created, so
        resuming keeps the original model. `thinking` enables DeepThink reasoning
        and `search` enables web search; both are independent of the model.

        `ref_file_ids` attaches images (from `upload_image`, already past
        `wait_for_file`) to this message — DeepSeek switches the thread's
        model_type to "vision" for these regardless of `model`, matching how
        the real app behaves when you attach a photo. Only confirmed to work
        starting a NEW thread (no `conversation_id`); attaching an image while
        resuming an existing thread is unverified, so it's rejected here rather
        than silently doing something we haven't seen actually work.
        """
        if conversation_id and model is not None:
            raise ValueError(
                "`model` cannot be set together with `conversation_id`; a thread's "
                "model is fixed when it is created. Pass `model` only on the first turn."
            )
        if conversation_id and ref_file_ids:
            raise ValueError(
                "Attaching images while resuming an existing conversation_id isn't "
                "supported (unverified against the real API) — start a new "
                "conversation for image messages."
            )
        session_id, parent_id = _decode_cid(conversation_id)
        if session_id is None:
            # New thread: select the model (default when unspecified), unless
            # images are attached — DeepSeek's vision model always wins then.
            session_id = self.create_chat_session()
            model_type: Optional[str] = "vision" if ref_file_ids else (model or DEFAULT_MODEL_TYPE)
        else:
            # Resuming: let the existing thread's model stand (send no model_type).
            model_type = None
        return _Stream(self, prompt, session_id, parent_id, model_type, thinking, search,
                       ref_file_ids)

    def chat(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
        ref_file_ids: Optional[List[str]] = None,
    ) -> Reply:
        """Return the complete reply (`.text`) plus its `.conversation_id`.
        If `thinking=True`, DeepThink's reasoning trace (if any came through)
        is on `.reasoning` — separate from the final answer in `.text`."""
        s = self.stream(prompt, conversation_id=conversation_id,
                        model=model, thinking=thinking, search=search,
                        ref_file_ids=ref_file_ids)
        content_parts, reasoning_parts = [], []
        for kind, text in s.events():
            (content_parts if kind == "content" else reasoning_parts).append(text)
        return Reply(text="".join(content_parts), conversation_id=s.conversation_id,
                     reasoning="".join(reasoning_parts))

    def close(self) -> None:
        self._http.close()


class _Stream:
    """Iterable of reply-text chunks. After it's consumed, `.conversation_id`
    holds the token for resuming the conversation."""

    def __init__(self, client: "DeepSeekClient", prompt: str, session_id: str,
                 parent_id: Optional[int], model: str,
                 thinking: bool, search: bool, ref_file_ids: Optional[List[str]] = None):
        self._client = client
        self._prompt = prompt
        self._session_id = session_id
        self._parent_id = parent_id
        self._model = model
        self._thinking = thinking
        self._search = search
        self._ref_file_ids = ref_file_ids or []
        self._message_id: Optional[int] = None

    def __iter__(self) -> Iterator[str]:
        """Plain text content only (backward compatible) — DeepThink reasoning,
        if any, is silently skipped here. Use `.events()` to get both."""
        for kind, text in self.events():
            if kind == "content":
                yield text

    def events(self) -> Iterator[tuple]:
        """Yield `(kind, text)` pairs — kind is `"content"` (the final answer)
        or `"reasoning"` (DeepThink's thinking trace, only present when
        `thinking=True` was passed). Reading `.conversation_id` afterwards
        still works the same either way."""
        body = {
            "chat_session_id": self._session_id,
            "parent_message_id": self._parent_id,
            "prompt": self._prompt,
            "ref_file_ids": self._ref_file_ids,
            "thinking_enabled": self._thinking,
            "search_enabled": self._search,
            "action": None,
            "preempt": False,
        }
        # Only select a model on a new thread; on resume the thread keeps its own.
        if self._model is not None:
            body["model_type"] = self._model
        # PoW challenges are short-lived, so solve right before the request.
        headers = {"x-ds-pow-response": self._client._pow_header()}
        meta: dict = {}
        with self._client._http.stream(
            "POST", COMPLETION_PATH, json=body, headers=headers
        ) as resp:
            resp.raise_for_status()
            yield from _parse_sse(resp.iter_lines(), meta)
        if meta.get("message_id") is not None:
            self._message_id = meta["message_id"]

    @property
    def conversation_id(self) -> str:
        return _encode_cid(self._session_id, self._message_id)


def _parse_sse(lines, meta: Optional[dict] = None) -> Iterator[tuple]:
    """Turn DeepSeek's SSE completion stream into `(kind, text)` deltas.

    The stream sends an initial snapshot frame whose `v` is the full response
    object (with `fragments[].content`), then a series of append frames:
      * {"p":"response/fragments/-1/content","o":"APPEND","v":" what"}  (sets path)
      * {"v":"'s"}                                                       (appends to it)
    We track the active append path and emit RESPONSE-fragment text as
    ("content", text) and THINK/THINKING-fragment text (DeepThink's reasoning,
    when thinking=True) as ("reasoning", text).

    NOTE: "RESPONSE" was confirmed by testing against the live API; the
    reasoning fragment's `type` string was NOT — "THINK"/"THINKING" is a
    best guess based on common naming in similar DeepSeek reverse-engineering
    projects. If reasoning text doesn't show up with thinking=True, set
    DEEPSEEK_DEBUG_FRAGMENTS=1 to print every fragment `type` seen to stderr,
    find the real one, and add it to _REASONING_FRAGMENT_TYPES below.

    If `meta` is given, the assistant's `message_id` is recorded into it (used to
    build the resumable conversation_id). The exact field location can vary, so
    we look in a few plausible spots defensively.
    """
    debug = bool(os.environ.get("DEEPSEEK_DEBUG_FRAGMENTS"))
    active_path: Optional[str] = None
    active_kind: Optional[str] = None
    emitted_initial = False

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        v = obj.get("v")

        # Snapshot frame: full response object.
        if isinstance(v, dict) and "response" in v:
            if meta is not None:
                _capture_message_id(meta, v)
            for frag in v["response"].get("fragments", []):
                ftype = frag.get("type")
                kind = _FRAGMENT_KIND.get(ftype)
                if debug and ftype not in _FRAGMENT_KIND:
                    print(f"[deepseek debug] unhandled fragment type: {ftype!r}", file=sys.stderr)
                if kind and frag.get("content"):
                    active_path = "response/fragments/-1/content"
                    active_kind = kind
                    if not emitted_initial:
                        emitted_initial = True
                        yield (kind, frag["content"])
            continue

        # Path-setting append frame.
        if "p" in obj:
            active_path = obj["p"]
            if meta is not None and active_path.endswith("message_id") \
                    and isinstance(v, int):
                meta["message_id"] = v
            if obj.get("o") == "APPEND" and isinstance(v, str) \
                    and active_path.endswith("content") and active_kind:
                yield (active_kind, v)
            continue

        # Bare append to the current path.
        if isinstance(v, str) and active_path and active_path.endswith("content") and active_kind:
            yield (active_kind, v)


def _capture_message_id(meta: dict, snapshot: dict) -> None:
    """Best-effort: pull the assistant message_id out of a snapshot frame.

    DeepSeek nests the assistant message under `response`; we check there first,
    then the snapshot root, accepting `message_id` or `id`.
    """
    for container in (snapshot.get("response"), snapshot):
        if isinstance(container, dict):
            mid = container.get("message_id", container.get("id"))
            if isinstance(mid, int):
                meta["message_id"] = mid
                return
