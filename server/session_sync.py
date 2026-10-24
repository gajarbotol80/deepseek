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

Enhancements:
  - Retries with short backoff on transient network errors
  - Validates pulled data before accepting it
  - Never pushes a clearly invalid/empty session
  - Soft-delete support (calls DELETE on the PHP side)
  - Clear logging of push/pull outcomes
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from deepseek.auth import Session

from .config import SESSION_SYNC_KEY, SESSION_SYNC_URL

log = logging.getLogger("deepseek.session_sync")


def enabled() -> bool:
    return bool(SESSION_SYNC_URL)


def _valid_token(token: object) -> bool:
    return isinstance(token, str) and len(token.strip()) >= 20


def push_session(session: Session, retries: int = 3) -> None:
    """Send the session to SESSION_SYNC_URL for safekeeping.

    Raises on permanent failure after retries. Callers decide whether that
    should block the response or just be logged (see server/api.py /session).
    """
    if not enabled():
        return
    if not _valid_token(session.token):
        raise RuntimeError("Refusing to push an invalid/empty session token")

    payload = {
        "key": SESSION_SYNC_KEY,
        "token": session.token,
        "cookies": session.cookies,
        "user_agent": session.user_agent,
        "captured_at": session.captured_at,
    }

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = httpx.post(SESSION_SYNC_URL, json=payload, timeout=15.0)
            if r.status_code == 409:
                # PHP refused to overwrite a newer session — treat as success
                # (our local copy is older; remote already has something better)
                log.info("push_session: remote has a newer session (409) — keeping remote")
                return
            r.raise_for_status()
            body = r.json()
            if not body.get("ok", False):
                raise RuntimeError(f"save.php rejected the session: {body.get('error', body)}")
            log.info("push_session: ok (attempt %d)", attempt)
            return
        except Exception as e:
            last_err = e
            log.warning("push_session attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(0.6 * attempt)

    raise RuntimeError(f"push_session failed after {retries} attempts: {last_err}")


def pull_session(retries: int = 2) -> Optional[Session]:
    """Best-effort restore from SESSION_SYNC_URL.

    Returns None (never raises) if sync isn't configured, the endpoint has
    nothing usable, or the request fails — callers treat that exactly like
    "no session found".
    """
    if not enabled():
        return None

    for attempt in range(1, retries + 1):
        try:
            r = httpx.get(
                SESSION_SYNC_URL,
                params={"key": SESSION_SYNC_KEY},
                timeout=15.0,
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                log.warning("pull_session: endpoint returned ok=false: %s", body.get("error"))
                return None
            data = body.get("data")
            if not data:
                log.info("pull_session: no session stored remotely yet")
                return None
            token = data.get("token", "")
            if not _valid_token(token):
                log.warning("pull_session: remote data has invalid token — ignoring")
                return None
            session = Session(
                token=token.strip(),
                cookies=data.get("cookies") or {},
                user_agent=data.get("user_agent") or "",
                captured_at=float(data.get("captured_at") or 0.0),
            )
            log.info(
                "pull_session: restored session (age≈%.0fs, %d cookies)",
                session.age,
                len(session.cookies),
            )
            return session
        except Exception as e:
            log.warning("pull_session attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(0.5 * attempt)

    return None


def soft_delete_remote() -> bool:
    """Ask the PHP side to soft-delete the main session (moves it into backups).

    Returns True on success, False otherwise. Never raises.
    """
    if not enabled():
        return False
    try:
        r = httpx.delete(
            SESSION_SYNC_URL,
            params={"key": SESSION_SYNC_KEY},
            timeout=10.0,
        )
        if r.status_code == 200 and r.json().get("ok"):
            log.info("soft_delete_remote: ok")
            return True
        log.warning("soft_delete_remote: unexpected response %s", r.status_code)
    except Exception as e:
        log.warning("soft_delete_remote failed: %s", e)
    return False
