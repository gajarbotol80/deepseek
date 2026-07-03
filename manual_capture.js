/*
 * manual_capture.js — grabs your signed-in DeepSeek session without Playwright.
 *
 * WHY: deepseek/auth.py normally uses Playwright + Chromium to sign in and read
 * `localStorage.userToken` + cookies. Chromium doesn't run on Android/Termux.
 * But a Session is really just three plain values — a token, a cookie string,
 * and a user-agent — and any signed-in browser tab already has all three. This
 * script reads them and shows them as one line of JSON you can copy.
 *
 * HOW TO USE (no PC required):
 *   1. On your phone's normal browser (Chrome, Firefox, etc.), open
 *      https://chat.deepseek.com and sign in / solve the human-check as usual.
 *   2. Copy the ONE-LINE "javascript:..." bookmarklet below (from "javascript:"
 *      to the end).
 *   3. Tap the address bar (while the DeepSeek tab is open and focused), paste
 *      it in place of the URL, and press Go / Enter.
 *      (Some browsers block "javascript:" pasted directly — if so, save it as
 *      a bookmark first, edit the bookmark's URL to this script, then tap the
 *      bookmark while on the DeepSeek tab.)
 *   4. A popup shows one line of JSON. Select all, copy it.
 *   5. On the device running this project (e.g. inside Termux), run:
 *          python manual_login.py
 *      and paste that line when asked.
 *
 * CAVEAT: this only sees cookies that are readable by JavaScript. If DeepSeek's
 * WAF/anti-bot cookie is marked HttpOnly, it won't show up here, and requests
 * from this captured session may get blocked by the WAF even though the token
 * is valid. If that happens, you'll see it immediately (401/403 on first
 * request) — see MANUAL_SESSION.md for a more thorough (proxy-based) capture
 * method as a fallback.
 */

javascript:(function(){try{var r=localStorage.getItem('userToken');if(!r){alert('No userToken found — make sure you are signed in to chat.deepseek.com in THIS tab first.');return;}var t=JSON.parse(r).value;if(!t){alert('userToken was present but empty — try refreshing the page after signing in.');return;}var o=JSON.stringify({token:t,cookies:document.cookie,ua:navigator.userAgent});window.prompt('Select all + copy this line, then paste it into manual_login.py:',o);}catch(e){alert('Capture failed: '+e.message);}})();

/* --- Same logic, unminified, for reference / manual console use --- */
(function () {
  try {
    const raw = localStorage.getItem('userToken');
    if (!raw) {
      alert('No userToken found — make sure you are signed in to chat.deepseek.com in THIS tab first.');
      return;
    }
    const token = JSON.parse(raw).value;
    if (!token) {
      alert('userToken was present but empty — try refreshing the page after signing in.');
      return;
    }
    const out = JSON.stringify({
      token: token,
      cookies: document.cookie,
      ua: navigator.userAgent,
    });
    window.prompt('Select all + copy this line, then paste it into manual_login.py:', out);
  } catch (e) {
    alert('Capture failed: ' + e.message);
  }
})();
