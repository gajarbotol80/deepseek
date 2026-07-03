"""Backs the session up to (and restores it from) an external PHP endpoint.

Why this exists: Render's free tier has no persistent disk — session/session.json
survives fine while the instance is warm, but a cold-start after inactivity
gets a fresh filesystem, and the session is just gone. Re-pasting it by hand
via /session works but is tedious to redo every time.

The fix: store a copy on any host with an actual persistent disk (a cheap
shared PHP host works fine — see save.php in this repo). This server pushes
to it whenever a new session is saved, and pulls from it automatically
whenever it wakes up with no usable local session.

Entirely optional — inactive unless SESSION_SYNC_URL is set.
"""

from __future__ import annotations

from typing import Optional

import httpx

from deepseek.auth import Session

from .config import SESSION_SYNC_KEY, SESSION_SYNC_URL


def enabled() -> bool:
    return bool(SESSION_SYNC_URL)


def push_session(session: Session) -> None:
    """Best-effort: send the session to SESSION_SYNC_URL for safekeeping.
    Raises on failure — caller decides whether that should block the response
    or just get logged (see server/api.py's /session handler)."""
    if not enabled():
        return
    payload = {
        "key": SESSION_SYNC_KEY,
        "token": session.token,
        "cookies": session.cookies,
        "user_agent": session.user_agent,
        "captured_at": session.captured_at,
    }
    r = httpx.post(SESSION_SYNC_URL, json=payload, timeout=15.0)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok", False):
        raise RuntimeError(f"save.php rejected the session: {body.get('error', body)}")


def pull_session() -> Optional[Session]:
    """Best-effort restore from SESSION_SYNC_URL. Returns None (never raises)
    if sync isn't configured, the endpoint has nothing stored yet, or the
    request fails for any reason — callers should treat that exactly like
    "no session found" and fall through to their normal no-session handling."""
    if not enabled():
        return None
    try:
        r = httpx.get(SESSION_SYNC_URL, params={"key": SESSION_SYNC_KEY}, timeout=15.0)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok") or not body.get("data"):
            return None
        data = body["data"]
        return Session(
            token=data["token"],
            cookies=data.get("cookies", {}),
            user_agent=data.get("user_agent", ""),
            captured_at=data.get("captured_at", 0.0),
        )
    except Exception:
        return None
