"""
OpenAI-compatible FastAPI server for DeepSeek.

Point any OpenAI client at http://localhost:8000/v1 :

    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")
    r = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "Hello!"}],
    )

Endpoints:
    GET  /v1/models
    POST /v1/chat/completions   (stream=true supported)
    GET  /healthz

Requests under /v1 are rate limited per client IP (default 30/min, set via
RATE_LIMIT_PER_MINUTE); /healthz is exempt.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import Body, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from deepseek.auth import DEFAULT_SESSION_FILE, LoginRequired, Session
from deepseek.client import DeepSeekClient

from . import session_sync
from .config import (
    ADMIN_TOKEN,
    MODEL_MAP,
    RATE_LIMIT_PER_MINUTE,
    SERVER_INTERACTIVE_LOGIN,
    is_known_model,
    resolve_model_type,
)
from .openai_format import completion_response, extract_images, messages_to_prompt, stream_chunks
from .ratelimit import RateLimiter, install_rate_limit
from .schemas import ChatCompletionRequest

load_dotenv()

app = FastAPI(title="DeepSeek OpenAI-compatible API", version="0.1.0")

# Lets a browser-based frontend (served from a different origin — a static
# host, file://, or a Claude artifact) call this API directly. Restrict this
# via ALLOWED_ORIGINS (comma-separated) once you have a fixed frontend origin;
# "*" is fine for personal single-user use, which is this project's target.
_allowed_origins = os.getenv("ALLOWED_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allowed_origins == "*" else _allowed_origins.split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_rate_limit(app, RateLimiter(limit=RATE_LIMIT_PER_MINUTE, window=60.0))

# One shared client (and its signed-in session) built lazily on first use.
_client: DeepSeekClient | None = None
_client_lock = threading.Lock()


def get_client() -> DeepSeekClient:
    """Build (once) the shared client and its signed-in session.

    Session resolution: cached file → (if that's missing/stale and
    SESSION_SYNC_URL is set) pulled from the external backup → headless
    capture off the persistent profile. If none of that works and
    SERVER_INTERACTIVE_LOGIN is on (the default), it opens a visible browser
    window so you can sign in — the triggering request blocks until you
    finish. If interactive login is off, it raises `LoginRequired`, which the
    endpoint turns into an actionable 503.

    This touches Playwright's sync API, so callers must invoke it OFF the event
    loop (via run_in_threadpool); calling it inside the asyncio loop raises
    "Playwright Sync API inside the asyncio loop"."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    _client = DeepSeekClient(allow_interactive=SERVER_INTERACTIVE_LOGIN)
                except LoginRequired:
                    restored = session_sync.pull_session()
                    if restored is None:
                        raise
                    restored.save(DEFAULT_SESSION_FILE)
                    _client = DeepSeekClient(allow_interactive=SERVER_INTERACTIVE_LOGIN)
    return _client


def reset_client() -> None:
    """Drop the cached client so the next request builds a fresh one — used
    after /session saves a newly captured session, so it takes effect
    immediately instead of waiting for a restart."""
    global _client
    with _client_lock:
        _client = None


def _error(message: str, status: int = 500, err_type: str = "server_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type}},
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _check_admin_token(request: Request) -> Optional[JSONResponse]:
    """If ADMIN_TOKEN is set, require it as `?token=` (GET) or in the POST body.
    Returns an error response to short-circuit with, or None if OK."""
    if not ADMIN_TOKEN:
        return None
    supplied = request.query_params.get("token", "")
    if supplied == ADMIN_TOKEN:
        return None
    return _error("Missing or incorrect ?token=", status=401, err_type="unauthorized")


_SESSION_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Session — DeepSeek Bridge</title>
<style>
  body{{background:#15171B;color:#EDECE6;font-family:'JetBrains Mono',monospace;
       max-width:520px;margin:40px auto;padding:0 16px;line-height:1.6;}}
  h1{{font-size:15px;letter-spacing:.04em;}}
  p{{color:#8A8D94;font-size:13px;}}
  textarea{{width:100%;height:110px;background:#1D2026;border:1px solid #2A2E36;
       color:#EDECE6;border-radius:8px;padding:10px;font-family:inherit;font-size:12px;}}
  input{{width:100%;background:#1D2026;border:1px solid #2A2E36;color:#EDECE6;
       border-radius:8px;padding:9px 10px;font-family:inherit;font-size:12px;margin-top:8px;}}
  button{{margin-top:12px;background:#C9A15A22;border:1px solid #C9A15A;color:#C9A15A;
       border-radius:8px;padding:9px 16px;font-family:inherit;font-size:12px;cursor:pointer;}}
  button:hover{{background:#C9A15A;color:#15171B;}}
  #out{{margin-top:14px;font-size:12px;white-space:pre-wrap;}}
  .ok{{color:#7FBF8F;}} .err{{color:#C9615A;}}
  code{{background:#1D2026;padding:1px 5px;border-radius:4px;}}
</style></head>
<body>
  <h1>DEEPSEEK BRIDGE — SESSION</h1>
  <p>Paste the JSON line from <code>manual_capture.js</code> below. This
     replaces whatever session the server currently has, and takes effect
     immediately — no redeploy needed.</p>
  <textarea id="data" placeholder='{{"token":"...","cookies":"...","ua":"..."}}'></textarea>
  {token_field}
  <button id="submit">Save session</button>
  <div id="out"></div>
<script>
  document.getElementById('submit').onclick = async () => {{
    const out = document.getElementById('out');
    out.className = ''; out.textContent = 'saving…';
    let payload;
    try {{ payload = JSON.parse(document.getElementById('data').value); }}
    catch(e) {{ out.className='err'; out.textContent='Not valid JSON: ' + e.message; return; }}
    const tokenField = document.getElementById('admin_token');
    const qs = tokenField ? ('?token=' + encodeURIComponent(tokenField.value)) : '';
    try {{
      const res = await fetch('/session' + qs, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }});
      const j = await res.json();
      if (res.ok) {{ out.className='ok'; out.textContent = j.message; }}
      else {{ out.className='err'; out.textContent = (j.error && j.error.message) || 'Failed'; }}
    }} catch(e) {{ out.className='err'; out.textContent = 'Request failed: ' + e.message; }}
  }};
</script>
</body></html>"""


@app.get("/session", response_class=HTMLResponse)
def session_page(request: Request):
    denied = _check_admin_token(request)
    if denied is not None:
        return HTMLResponse("<p style='font-family:monospace'>Missing or incorrect ?token=</p>", status_code=401)
    token_field = (
        f'<input id="admin_token" type="hidden" value="{ADMIN_TOKEN}">' if ADMIN_TOKEN else ""
    )
    return HTMLResponse(_SESSION_PAGE.format(token_field=token_field))


@app.post("/session")
def session_save(request: Request, payload: dict = Body(...)):
    denied = _check_admin_token(request)
    if denied is not None:
        return denied

    token = (payload.get("token") or "").strip()
    cookie_header = payload.get("cookies") or ""
    user_agent = payload.get("ua") or payload.get("user_agent") or ""

    if not token:
        return _error("No `token` in the pasted data — were you signed in to "
                       "chat.deepseek.com when you ran the bookmarklet?",
                       status=400, err_type="invalid_request_error")

    session = Session.from_manual(token, cookie_header, user_agent)
    session.save(DEFAULT_SESSION_FILE)
    reset_client()

    n_cookies = len(session.cookies)
    message = f"Session saved ({n_cookies} cookie(s) captured). It's live now — try a chat request."

    if session_sync.enabled():
        try:
            session_sync.push_session(session)
            message += " Also backed up externally, so it'll survive a cold start."
        except Exception as e:
            message += f" (Warning: external backup failed — {e})"

    return {"message": message}


@app.get("/v1/models")
def list_models():
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": created, "owned_by": "deepseek"}
            for name in MODEL_MAP
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if not req.messages:
        return _error("`messages` must not be empty", status=400, err_type="invalid_request_error")

    if not is_known_model(req.model):
        return _error(
            f"The model `{req.model}` does not exist. Available models: "
            f"{', '.join(MODEL_MAP)}",
            status=404, err_type="model_not_found",
        )

    # A thread's model is fixed when it's created, so on resume we ignore `model`
    # (the OpenAI SDK always sends one) and let the existing thread's model stand.
    model_type = None if req.conversation_id else resolve_model_type(req.model)
    prompt = messages_to_prompt(req.messages)

    images = extract_images(req.messages)
    if images and req.conversation_id:
        return _error(
            "Images can only be sent as the first message of a new conversation "
            "(omit conversation_id). Attaching one mid-thread isn't supported.",
            status=400, err_type="invalid_request_error",
        )

    try:
        # Off the event loop: get_client() uses Playwright's sync API, which
        # errors if run inside the asyncio loop.
        client = await run_in_threadpool(get_client)
    except LoginRequired as e:
        return _error(str(e), status=503, err_type="login_required")
    except Exception as e:  # session/login failure
        return _error(f"Failed to initialise DeepSeek session: {e}")

    ref_file_ids: list[str] = []
    if images:
        try:
            for img in images:
                file_id = await run_in_threadpool(
                    client.upload_image, img.data, img.filename, img.content_type
                )
                await run_in_threadpool(client.wait_for_file, file_id)
                ref_file_ids.append(file_id)
        except Exception as e:
            return _error(f"Image upload failed: {e}", status=502, err_type="upload_error")

    if req.stream:
        def gen():
            stream = client.stream(
                prompt, conversation_id=req.conversation_id,
                model=model_type, thinking=req.thinking, search=req.search,
                ref_file_ids=ref_file_ids,
            )
            yield from stream_chunks(req.model, stream)

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        reply = await run_in_threadpool(
            client.chat, prompt, req.conversation_id,
            model_type, req.thinking, req.search, ref_file_ids,
        )
    except Exception as e:
        return _error(f"DeepSeek request failed: {e}")

    return completion_response(req.model, reply.text, prompt, reply.conversation_id,
                               reasoning=reply.reasoning)
