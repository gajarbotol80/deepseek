"""Translate between OpenAI's chat-completions shapes and our DeepSeek client.

DeepSeek's protocol has no system/role channel — just a single `prompt` string.
So we flatten the OpenAI `messages` array into one prompt, and wrap DeepSeek's
text output back into OpenAI response/stream objects.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Iterable, List, Optional

from .schemas import ChatMessage

_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant"}
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w/.+-]+);base64,(?P<data>.+)$", re.DOTALL)


def _text_of(content) -> str:
    """Extract plain text from a message's content (string or list-of-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


class ExtractedImage:
    __slots__ = ("data", "content_type", "filename")

    def __init__(self, data: bytes, content_type: str, filename: str):
        self.data = data
        self.content_type = content_type
        self.filename = filename


def extract_images(messages: List[ChatMessage]) -> List[ExtractedImage]:
    """Pull image attachments out of the LAST message (only the newest turn
    can carry new images — see the note on `DeepSeekClient.stream`'s
    `ref_file_ids`). Only base64 data URLs are supported (`data:image/...;
    base64,...`) — the standard shape the OpenAI vision message format uses
    and what any browser-based client (including our own frontend) produces
    natively via FileReader. Plain http(s) image URLs are NOT fetched, to
    avoid turning this server into an open fetch-any-URL proxy; ignored
    silently if present.
    """
    if not messages:
        return []
    last = messages[-1]
    if last.role != "user" or not isinstance(last.content, list):
        return []

    images: List[ExtractedImage] = []
    for i, part in enumerate(last.content):
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        url = (part.get("image_url") or {}).get("url", "")
        m = _DATA_URL_RE.match(url)
        if not m:
            continue
        mime = m.group("mime")
        try:
            data = base64.b64decode(m.group("data"))
        except Exception:
            continue
        ext = mime.split("/")[-1].split("+")[0] or "jpg"
        images.append(ExtractedImage(data=data, content_type=mime,
                                      filename=f"image-{i}.{ext}"))
    return images


def messages_to_prompt(messages: List[ChatMessage]) -> str:
    """Flatten a chat history into a single prompt DeepSeek can answer.

    A lone user message is sent verbatim. Multi-turn / system-prompted
    conversations are serialised with role labels and a trailing 'Assistant:'
    cue so the model continues in the right voice.
    """
    if len(messages) == 1 and messages[0].role == "user":
        return _text_of(messages[0].content)

    lines = []
    for m in messages:
        label = _ROLE_LABELS.get(m.role, m.role.capitalize())
        lines.append(f"{label}: {_text_of(m.content)}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — DeepSeek's web API gives us no count."""
    return max(1, len(text) // 4)


def completion_response(model: str, content: str, prompt: str,
                        conversation_id: str = None, reasoning: str = "") -> dict:
    """A full (non-streaming) OpenAI chat.completion object.

    `conversation_id` is an extra top-level field (outside OpenAI's schema) you
    send back to resume the conversation. `reasoning` (DeepThink's trace, when
    thinking=True) is attached as `message.reasoning_content` if non-empty,
    mirroring DeepSeek's own official API field name for reasoner models.
    """
    pt, ct = _est_tokens(prompt), _est_tokens(content)
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def stream_chunks(model: str, stream: Iterable) -> Iterable[str]:
    """Yield OpenAI SSE lines (`data: {...}\\n\\n`) for a streamed completion.

    `stream` is the client's `_Stream` object; we read it via `.events()` so
    DeepThink's reasoning text (when thinking=True) comes through as
    `delta.reasoning_content`, separate from the final answer in
    `delta.content` — the same field name DeepSeek's own official API uses
    for reasoner models, so existing OpenAI-compatible UIs that already know
    to render `reasoning_content` distinctly will do the right thing here too.
    After the stream is consumed we read its `.conversation_id` and attach it
    to the final chunk.
    """
    cid, created = _id(), _now()

    def frame(delta: dict, finish=None, extra: dict = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    # First frame announces the assistant role.
    yield frame({"role": "assistant", "content": ""})
    for kind, text in stream.events():
        if not text:
            continue
        if kind == "reasoning":
            yield frame({"reasoning_content": text})
        else:
            yield frame({"content": text})
    conversation_id = getattr(stream, "conversation_id", None)
    yield frame({}, finish="stop", extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"
