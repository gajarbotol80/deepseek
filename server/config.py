"""Server configuration: the OpenAI-facing model names and what they map to."""

import os

# Requests per minute allowed per client IP (override with RATE_LIMIT_PER_MINUTE).
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

# When the server has no session, should it pop a visible browser window for
# interactive sign-in (the first request then blocks until you finish logging
# in)? On by default for local single-user use. Set to "0"/"false" for headless
# deployments, where it instead returns a 503 telling the caller to run
# `python -m deepseek.auth`.
SERVER_INTERACTIVE_LOGIN = os.getenv("SERVER_INTERACTIVE_LOGIN", "1").lower() not in (
    "0", "false", "no", "off",
)

# Protects the /session page (where a captured session gets pasted in on
# headless deployments like Render, where there's no shell/disk access worth
# relying on). If unset, that page is open to anyone with the URL — fine for
# quick testing, but set this once the service has a public URL.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Optional external backup for the session, on a host with an actual
# persistent disk (e.g. a small PHP host) — see server/session_sync.py.
# Render's free tier wipes its disk on every restart/cold-start, so without
# this, the server "forgets" its session every time it sleeps and needs a
# fresh manual /session paste. When set, a saved session is also pushed here,
# and a missing/expired local session is pulled from here automatically
# before falling back to asking for a manual paste.
SESSION_SYNC_URL = os.getenv("SESSION_SYNC_URL", "")   # e.g. https://gajarbotol.site/save.php
SESSION_SYNC_KEY = os.getenv("SESSION_SYNC_KEY", "")   # shared secret with that endpoint

# Public model ids the server advertises (via /v1/models) and accepts, mapped to
# DeepSeek's `model_type` wire value. This is the MODEL axis ONLY — it picks
# which model answers. DeepThink and web Search are orthogonal tools requested
# per call via `tool_names` (see deepseek.client.KNOWN_TOOLS), never encoded in
# the model name.
#
# "vision" is deferred: it only does anything with an image attached, which needs
# ref_file_ids / file-upload plumbing we don't have yet.
MODEL_MAP = {
    "deepseek-chat":   "default",   # Instant — the fast default model
    "deepseek-expert": "expert",    # Expert  — the stronger, slower model
}

DEFAULT_MODEL = "deepseek-chat"


def is_known_model(name: str) -> bool:
    """Whether `name` is a model id we accept (used to 404 unknown models)."""
    return name in MODEL_MAP


def resolve_model_type(name: str) -> str:
    """Translate a public model id to DeepSeek's `model_type` wire value.

    Caller must check `is_known_model` first; this raises KeyError otherwise.
    """
    return MODEL_MAP[name]
