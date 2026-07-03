"""
Manual session import — for devices where Playwright/Chromium can't run
(Android, Termux, restricted environments, etc).

This is the Termux-friendly replacement for `python -m deepseek.auth`. It does
NOT open a browser or touch Playwright at all — it just turns a JSON blob you
captured by hand (see manual_capture.js) into session/session.json, in exactly
the format `deepseek/auth.py` already expects. Nothing else in the project
needs to know a browser was never involved on this device.

Usage:
    1. Sign in at https://chat.deepseek.com on ANY browser (phone or PC).
    2. Run the manual_capture.js bookmarklet there and copy the JSON it shows.
    3. Run this script and paste that JSON when asked:
           python manual_login.py
    4. Now run the project as normal, e.g.:
           python app.py
       or, in Python:
           from deepseek import DeepSeekClient
           client = DeepSeekClient()

When the session eventually expires (DeepSeek starts rejecting requests with an
auth error), just repeat steps 1-3 to refresh it — there's no automatic
headless refresh here since that also needs a browser.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / "session" / "session.json"


def parse_cookie_header(cookie_header: str) -> dict:
    """Turn a raw `document.cookie` string ("a=1; b=2") into a {name: value} dict —
    the same shape Playwright's `context.cookies()` produces on the normal path."""
    cookies: dict[str, str] = {}
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies[name.strip()] = value.strip()
    return cookies


def main() -> None:
    print("Paste the JSON line from manual_capture.js below, then press Enter:\n")
    try:
        raw = input("> ").strip()
    except EOFError:
        raw = ""

    if not raw:
        print("Nothing pasted — aborting.")
        sys.exit(1)

    try:
        data = json.loads(raw)
        token = data["token"]
        cookie_header = data.get("cookies", "")
        user_agent = data.get("ua", "")
    except Exception as e:
        print(f"Couldn't parse that as the expected JSON: {e}")
        print("Make sure you copied the WHOLE line the popup showed you.")
        sys.exit(1)

    if not token:
        print("No token in the pasted data — were you actually signed in to "
              "chat.deepseek.com when you ran the bookmarklet?")
        sys.exit(1)

    cookies = parse_cookie_header(cookie_header)
    session = {
        "token": token,
        "cookies": cookies,
        "user_agent": user_agent,
        "captured_at": time.time(),
    }

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(session, indent=2), encoding="utf-8")

    print(f"\nSaved session to {SESSION_FILE}")
    print(f"Captured {len(cookies)} cookie(s). Token starts with: {token[:10]}...")
    if not cookies:
        print(
            "\nWarning: no cookies were captured. Some requests may still fail "
            "if DeepSeek's WAF check needs a cookie — see MANUAL_SESSION.md for "
            "a fallback capture method if you get 401/403 errors."
        )
    print("\nYou can now run `python app.py` or use DeepSeekClient() directly — "
          "no browser needed on this device.")


if __name__ == "__main__":
    main()
