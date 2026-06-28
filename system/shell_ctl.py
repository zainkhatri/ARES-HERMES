#!/usr/bin/env python3
"""ARES shell control service.

Tiny host-side HTTP endpoint that drives the persistent `web` tmux session that
ttyd renders. It exists because the dashboard runs inside LXC 101 and cannot reach
the host's tmux socket, while the phone (on the tailnet) can hit this directly the
same way it already hits ttyd at 100.77.42.110:7681.

tmux is the source of truth; the ttyd iframe is just an attached client, so every
command here is reflected live in the browser with no cross-origin iframe access.

No external deps (stdlib only). Hardened per the Power-of-Ten rules: arg-list
subprocess calls (never a shell string), fixed whitelists for keys/scroll/index,
explicit error returns, bounded work.
"""

import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Bound tailnet-only by topology: 100.77.42.110 is CGNAT, routable only over the
# tailnet — same trust boundary as `ssh ares` and the ttyd shell itself.
HOST = "100.77.42.110"
PORT = 7683
SESSION = "web"

ALLOWED_ORIGINS = {
    "http://100.77.42.110:8080",
    "https://ares.tail3045df.ts.net",
    "http://100.77.42.110:7681",
}

# Map of toolbar key names -> tmux send-keys tokens. Fixed whitelist: a request
# key not in here is rejected, so no arbitrary input ever reaches send-keys.
KEY_MAP = {
    "Escape": "Escape",
    "Tab": "Tab",
    "Enter": "Enter",
    "BSpace": "BSpace",
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",
    "C-c": "C-c",
    "C-d": "C-d",
    "C-l": "C-l",
    "C-r": "C-r",
    "C-z": "C-z",
}

INDEX_RE = re.compile(r"^[0-9]{1,3}$")
MAX_BODY = 4096

# Capture bounds for the native console transcript.
MAX_CAPTURE_LINES = 5000
DEFAULT_CAPTURE_LINES = 400


def _tmux(args):
    """Run `tmux <args>` as an arg list. Returns (ok, stdout_text)."""
    assert isinstance(args, list), "tmux args must be a list"
    try:
        out = subprocess.run(
            ["tmux", *args],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    if out.returncode != 0:
        return False, (out.stderr or "tmux error").strip()
    return True, out.stdout


def capture_pane(history_lines):
    """Return (ok, text): the active pane's content, with `history_lines` of
    scrollback above the visible screen. `-J` joins wrapped lines so the native
    side gets logical lines, not terminal-width fragments."""
    assert isinstance(history_lines, int), "history_lines must be int"
    assert 0 <= history_lines <= MAX_CAPTURE_LINES, "history_lines out of range"
    args = ["capture-pane", "-p", "-J", "-t", SESSION]
    if history_lines > 0:
        args += ["-S", f"-{history_lines}"]
    ok, raw = _tmux(args)
    if not ok:
        return False, raw
    return True, raw.rstrip("\n")


def list_windows():
    """Return [{index, name, active}] for the web session, or [] if none."""
    ok, raw = _tmux([
        "list-windows", "-t", SESSION,
        "-F", "#{window_index}|#{window_name}|#{window_active}",
    ])
    if not ok:
        return []
    windows = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        windows.append({
            "index": int(parts[0]) if parts[0].isdigit() else parts[0],
            "name": parts[1],
            "active": parts[2] == "1",
        })
    return windows


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- response helpers ---------------------------------------------------

    def _cors(self):
        origin = self.headers.get("Origin", "")
        allow = origin if origin in ALLOWED_ORIGINS else "*"
        self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- verbs --------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _capture_lines(self):
        """Clamp the ?lines= query param into [0, MAX_CAPTURE_LINES]."""
        q = parse_qs(urlparse(self.path).query)
        raw = q.get("lines", [str(DEFAULT_CAPTURE_LINES)])[0]
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_CAPTURE_LINES
        return max(0, min(MAX_CAPTURE_LINES, n))

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/windows":
            self._json({"windows": list_windows()})
            return
        if path == "/capture":
            ok, content = capture_pane(self._capture_lines())
            self._json({"ok": ok,
                        "content": content if ok else "",
                        "error": None if ok else content},
                       200 if ok else 500)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.rstrip("/")
        body = self._read_body()

        if path == "/new":
            # target picks what the new tmux window runs. Fixed whitelist (arg list,
            # no shell) so only these two hosts can ever be opened: ARES = a local
            # login shell on this host; NEXUS = `ssh nexus` (tailnet-reachable from here).
            target = str(body.get("target", "ares"))
            if target == "ares":
                args = ["new-window", "-t", SESSION]
            elif target == "nexus":
                args = ["new-window", "-t", SESSION, "-n", "nexus", "ssh", "nexus"]
            else:
                self._json({"error": "bad target"}, 400)
                return
            ok, msg = _tmux(args)
            self._json({"ok": ok, "error": None if ok else msg,
                        "windows": list_windows()}, 200 if ok else 500)
            return

        if path == "/select":
            idx = str(body.get("index", ""))
            if not INDEX_RE.match(idx):
                self._json({"error": "bad index"}, 400)
                return
            ok, msg = _tmux(["select-window", "-t", f"{SESSION}:{idx}"])
            self._json({"ok": ok, "error": None if ok else msg,
                        "windows": list_windows()}, 200 if ok else 500)
            return

        if path == "/kill":
            idx = str(body.get("index", ""))
            if not INDEX_RE.match(idx):
                self._json({"error": "bad index"}, 400)
                return
            ok, msg = _tmux(["kill-window", "-t", f"{SESSION}:{idx}"])
            self._json({"ok": ok, "error": None if ok else msg,
                        "windows": list_windows()}, 200 if ok else 500)
            return

        if path == "/text":
            # Inject literal text typed in the page's native input box, optionally
            # followed by Enter. This bypasses xterm.js's flaky mobile soft-keyboard
            # handling entirely — the phone types into a real <input>, we deliver it.
            text = body.get("text", "")
            if not isinstance(text, str) or len(text) > MAX_BODY:
                self._json({"error": "bad text"}, 400)
                return
            ok = True
            msg = None
            if text:
                # '-l' = send the literal string (no key-name parsing); '--' guards a
                # leading dash from being read as a flag. Arg list => no shell injection.
                ok, msg = _tmux(["send-keys", "-t", SESSION, "-l", "--", text])
            if ok and body.get("enter"):
                ok, msg = _tmux(["send-keys", "-t", SESSION, "Enter"])
            self._json({"ok": ok, "error": None if ok else msg}, 200 if ok else 500)
            return

        if path == "/key":
            key = body.get("key", "")
            token = KEY_MAP.get(key)
            if token is None:
                self._json({"error": "unknown key"}, 400)
                return
            ok, msg = _tmux(["send-keys", "-t", SESSION, token])
            self._json({"ok": ok, "error": None if ok else msg}, 200 if ok else 500)
            return

        if path == "/scroll":
            direction = body.get("dir", "")
            if direction == "up":
                # Enter copy-mode if not already, then page up one screen.
                _tmux(["copy-mode", "-t", SESSION])
                ok, msg = _tmux(["send-keys", "-t", SESSION, "-X", "-N", "1", "page-up"])
            elif direction == "down":
                ok, msg = _tmux(["send-keys", "-t", SESSION, "-X", "page-down"])
            elif direction in ("bottom", "exit"):
                ok, msg = _tmux(["send-keys", "-t", SESSION, "-X", "cancel"])
            else:
                self._json({"error": "bad dir"}, 400)
                return
            self._json({"ok": ok, "error": None if ok else msg}, 200 if ok else 500)
            return

        self._json({"error": "not found"}, 404)

    # Quieter logging — one line, no client hostname lookups.
    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ares-shell-ctl listening on http://{HOST}:{PORT} (session={SESSION})")
    server.serve_forever()


if __name__ == "__main__":
    main()
