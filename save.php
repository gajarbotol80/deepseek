<?php
/**
 * save.php — persistent session relay for the DeepSeek Bridge server.
 *
 * WHY THIS EXISTS: Render's free tier has no persistent disk. Every cold
 * start after inactivity wipes session/session.json, and the server "forgets"
 * it's signed in. This script lives on ordinary PHP hosting (which DOES keep
 * its disk), and just stores the last session it was given as a JSON file.
 * The Python server pushes to it whenever you paste a fresh session via
 * /session, and pulls from it automatically on a cold start — no database,
 * no shell access needed on Render's side.
 *
 * SETUP:
 *   1. Upload this file to your PHP host, e.g. https://gajarbotol.site/save.php
 *   2. Set a real secret below (SECRET_KEY) — this is a shared password
 *      between this script and your Python server. Anyone who knows it can
 *      read/overwrite your session, so treat it like one.
 *   3. On your Render service, set env vars:
 *        SESSION_SYNC_URL=https://gajarbotol.site/save.php
 *        SESSION_SYNC_KEY=<the same secret you set below>
 *   4. IMPORTANT: move SESSION_FILE below outside your public webroot if your
 *      host allows it (e.g. one level above public_html/), so the raw session
 *      file itself can never be fetched directly by URL. If your host only
 *      gives you one public folder, at minimum add the .htaccess shown at the
 *      bottom of this file to block direct access to the .json file.
 *
 * PROTOCOL:
 *   POST  { "key": "...", "token": "...", "cookies": {...}, "user_agent": "...", "captured_at": 123 }
 *         -> { "ok": true }
 *   GET   ?key=...
 *         -> { "ok": true, "data": { "token": "...", "cookies": {...}, ... } }
 *         -> { "ok": true, "data": null }   (nothing stored yet)
 *         -> { "ok": false, "error": "..." } (bad/missing key, or bad input)
 */

// ---- 1. CONFIGURE THIS ----
const SECRET_KEY = "CHANGE-ME-TO-A-LONG-RANDOM-STRING";

// Where the session gets stored. Move this outside the web-servable directory
// if you can (e.g. "../deepseek_session.json" if this script sits in
// public_html/). A random-looking filename is a weak fallback if you can't.
const SESSION_FILE = __DIR__ . "/deepseek_session_store.json";

// ---------------------------

header("Content-Type: application/json");

function fail(string $message, int $status = 400): void {
    http_response_code($status);
    echo json_encode(["ok" => false, "error" => $message]);
    exit;
}

function check_key(?string $supplied): void {
    if (SECRET_KEY === "CHANGE-ME-TO-A-LONG-RANDOM-STRING") {
        fail("save.php still has its placeholder SECRET_KEY — set a real one before using this.", 500);
    }
    if (!is_string($supplied) || !hash_equals(SECRET_KEY, $supplied)) {
        fail("Missing or incorrect key.", 401);
    }
}

$method = $_SERVER["REQUEST_METHOD"];

if ($method === "GET") {
    check_key($_GET["key"] ?? null);

    if (!file_exists(SESSION_FILE)) {
        echo json_encode(["ok" => true, "data" => null]);
        exit;
    }
    $raw = file_get_contents(SESSION_FILE);
    $data = json_decode($raw, true);
    if ($data === null) {
        fail("Stored session file is corrupt.", 500);
    }
    echo json_encode(["ok" => true, "data" => $data]);
    exit;
}

if ($method === "POST") {
    $body = json_decode(file_get_contents("php://input"), true);
    if (!is_array($body)) {
        fail("Body must be JSON.");
    }
    check_key($body["key"] ?? null);

    $token = $body["token"] ?? null;
    if (!is_string($token) || $token === "") {
        fail("Missing `token`.");
    }

    $record = [
        "token"       => $token,
        "cookies"     => $body["cookies"] ?? new stdClass(),
        "user_agent"  => $body["user_agent"] ?? "",
        "captured_at" => $body["captured_at"] ?? time(),
    ];

    $written = file_put_contents(SESSION_FILE, json_encode($record, JSON_PRETTY_PRINT));
    if ($written === false) {
        fail("Couldn't write the session file — check file permissions on this host.", 500);
    }
    // Best-effort: keep the stored file itself unreadable to other users on
    // shared hosting, where supported.
    @chmod(SESSION_FILE, 0600);

    echo json_encode(["ok" => true]);
    exit;
}

fail("Only GET and POST are supported.", 405);

/*
 * ---- Optional but recommended: .htaccess (Apache) in the SAME directory ----
 * Blocks direct browser access to the stored JSON file by name, in case you
 * couldn't move SESSION_FILE outside the webroot. Save as ".htaccess" next
 * to this script:
 *
 *   <Files "deepseek_session_store.json">
 *     Require all denied
 *   </Files>
 *
 * (Older Apache: use "Deny from all" instead of "Require all denied".)
 */
