# Running on Android / Termux (no Chromium)

The normal setup uses Playwright to drive a real Chromium browser for login,
because that's the only piece of this project that actually needs a browser —
`deepseek/client.py` (the part that talks to DeepSeek and streams replies) is
plain HTTP and doesn't touch a browser at all. Chromium just has no build for
Android, so Playwright can't run in Termux.

The fix: capture the three values a session actually is — a bearer token, a
cookie string, and a user-agent — by hand from any normal signed-in browser,
and hand them to the project directly. No Chromium, no Playwright, on the
device that runs the server.

## Setup

**1. Install Termux-friendly dependencies (skips Playwright):**

```bash
pip install -r requirements-termux.txt
python -c "import wasmtime; print('ok')"   # see note below if this fails
```

**2. Sign in somewhere normal.** Open `https://chat.deepseek.com` in your
phone's regular browser (Chrome, Firefox, whatever — it doesn't need to be the
same device running Termux) and sign in, solving the human-check as usual.

**3. Capture the session.** Open `manual_capture.js` and copy the one-line
`javascript:...` bookmarklet at the top. While the DeepSeek tab is open and
focused, paste it into the address bar in place of the URL and hit Go/Enter.
(If your browser refuses to run a pasted `javascript:` URL, save it as a
bookmark first, then tap the bookmark while on the DeepSeek tab — that always
works.) A popup shows one line of JSON — copy it.

**4. Import it.** On the device running the project:

```bash
python manual_login.py
```

Paste the JSON line when prompted. This writes `session/session.json` in
exactly the format the project already expects, so nothing downstream needs to
know a browser was never involved.

**5. Run it as normal:**

```bash
python app.py
# or
python -m deepseek.auth   # NOT used here — this is the Playwright path
```

## When the session expires

There's no automatic headless refresh on this path (that also needs a
browser). When DeepSeek starts rejecting requests with an auth error, just
repeat steps 2–4. You can raise how long a captured session is trusted before
the project re-checks it with `DEEPSEEK_SESSION_MAX_AGE` (seconds, env var) —
it doesn't make an expired token magically valid, it just skips pointless
rechecks.

## Two things that can go wrong

**Cookies:** `document.cookie` only sees cookies that aren't marked
`HttpOnly`. If DeepSeek's WAF/anti-bot cookie happens to be `HttpOnly`, this
capture method won't see it, and you may get a 401/403 even with a valid
token. You'll find out immediately (first request fails) rather than
silently. If that happens, the reliable fallback is capturing the *actual*
request headers a real browser sends — e.g. running a local proxy (mitmproxy)
in Termux, pointing your phone's browser at it, and reading the `cookie` and
`authorization` headers off a real request to chat.deepseek.com. That's more
setup; ask if you want it built out.

**wasmtime on Termux:** Every chat request needs to solve a small
proof-of-work challenge, done by running DeepSeek's own WASM module via the
`wasmtime` Python package. `wasmtime`'s prebuilt wheels target manylinux
(glibc); Termux's Python is built against Android's Bionic libc, so the wheel
can fail to import even though `pip install` succeeds. Run
`python -c "import wasmtime"` right after installing to check. If it fails,
this is a real platform gap — options are building wasmtime from source
against Termux's libc, or running the server itself on something with normal
Linux underneath (a small VPS, or Termux's `proot-distro` with an Ubuntu
chroot) while still reaching it from your phone.
