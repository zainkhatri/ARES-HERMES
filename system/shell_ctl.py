#!/usr/bin/env python3
"""HTTP control bridge: browser → host tmux.

Runs on the host (root) at 100.77.42.110:7683 so it can drive root's tmux
session. LXC 101 (Flask) cannot reach the host's tmux socket — this is the
workaround. The browser hits this directly over the tailnet, same trust
boundary as ttyd / ssh.

Endpoints (JSON in, JSON out):
  GET  /windows                 → list tmux windows
  POST /new    {target}         → new-window (ares bash | hermes ssh)
  POST /select {index}          → select-window
  POST /kill   {index}          → kill-window
  POST /key    {key}            → tmux send-keys (whitelisted names)
  POST /scroll {dir}            → scroll pane (up/down/bottom)
  POST /text   {text, enter}    → inject literal text + optional Enter
  POST /capture                 → return visible pane text (for copy)
"""
import http.server
import json
import os
import re
import subprocess
import sys

HOST = "100.77.42.110"
PORT = 7683
SESSION = "web"
HERMES_SSH = "zain@100.100.29.36"
BLOCK_RC = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/block-shell.rc"

ALLOWED_KEYS = {
    "Escape", "Tab", "Enter", "Up", "Down", "Left", "Right",
    "C-c", "C-d", "C-l", "C-r", "C-u", "C-a", "C-e", "C-k",
    "PPage", "NPage", "BSpace", "Home", "End",
}


def _run(*cmd, timeout=5):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.returncode


def _windows():
    out, rc = _run("tmux", "list-windows", "-t", SESSION,
                   "-F", "#{window_index}\t#{window_name}\t#{window_active}")
    if rc != 0:
        return []
    wins = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            wins.append({"index": int(parts[0]),
                         "name": parts[1], "active": parts[2] == "1"})
    return wins


def _active_index():
    out, _ = _run("tmux", "display-message", "-p", "-t", SESSION,
                  "#{window_index}")
    s = out.strip()
    return int(s) if s.isdigit() else 0


def _tgt(win=None):
    """Build a tmux target: session[:window]."""
    if win is not None and isinstance(win, int) and 0 <= win <= 999:
        return f"{SESSION}:{win}"
    return SESSION


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *_):
        pass  # suppress access log noise

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n)) if n > 0 else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/windows":
            self._json({"windows": _windows()})
        elif self.path == "/health":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            d = self._body()
        except Exception:
            self._json({"error": "bad json"}, 400)
            return

        win = d.get("win") if isinstance(d.get("win"), int) else None
        tgt = _tgt(win)

        if self.path == "/new":
            target = d.get("target", "ares")
            if target == "hermes":
                subprocess.run(
                    ["tmux", "new-window", "-t", SESSION, "-n", "hermes",
                     "ssh", HERMES_SSH],
                    capture_output=True)
            else:
                result = subprocess.run(
                    ["tmux", "new-window", "-P", "-F", "#{window_index}",
                     "-t", SESSION, "-n", "shell",
                     f"bash --rcfile {BLOCK_RC} -i"],
                    capture_output=True, text=True)
                new_idx = (result.stdout or "").strip()
                if new_idx.isdigit():
                    subprocess.run(
                        ["tmux", "set-option", "-w", "-t", f"{SESSION}:{new_idx}", "automatic-rename", "off"],
                        capture_output=True)
            self._json({"windows": _windows()})

        elif self.path == "/select":
            idx = str(d.get("index", "0"))
            if idx.isdigit():
                subprocess.run(
                    ["tmux", "select-window", "-t", f"{SESSION}:{idx}"],
                    capture_output=True)
            self._json({"windows": _windows()})

        elif self.path == "/kill":
            idx = str(d.get("index", "0"))
            if idx.isdigit():
                subprocess.run(
                    ["tmux", "kill-window", "-t", f"{SESSION}:{idx}"],
                    capture_output=True)
            self._json({"windows": _windows()})

        elif self.path == "/key":
            key = str(d.get("key", ""))
            if key in ALLOWED_KEYS:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tgt, key],
                    capture_output=True)
            self._json({"ok": True})

        elif self.path == "/scroll":
            direction = d.get("dir", "")
            if direction == "up":
                subprocess.run(["tmux", "copy-mode", "-t", tgt],
                               capture_output=True)
                subprocess.run(
                    ["tmux", "send-keys", "-t", tgt, "-X", "halfpage-up"],
                    capture_output=True)
            elif direction == "down":
                subprocess.run(
                    ["tmux", "send-keys", "-t", tgt, "-X", "halfpage-down"],
                    capture_output=True)
            elif direction == "bottom":
                # q exits copy-mode cleanly; if not in copy-mode it's a no-op
                subprocess.run(
                    ["tmux", "send-keys", "-t", tgt, "q"],
                    capture_output=True)
                subprocess.run(
                    ["tmux", "send-keys", "-t", tgt, "G"],
                    capture_output=True)
            self._json({"ok": True})

        elif self.path == "/text":
            text = str(d.get("text", ""))[:10000]
            enter = bool(d.get("enter", False))
            if text:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tgt, "-l", "--", text],
                    capture_output=True)
            if enter:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tgt, "Enter"],
                    capture_output=True)
            self._json({"ok": True})

        elif self.path == "/capture":
            # Return visible pane content for copy-to-clipboard
            out, rc = _run(
                "tmux", "capture-pane", "-p", "-t", tgt, "-J",
                timeout=4)
            if rc == 0:
                self._json({"text": out})
            else:
                self._json({"text": "", "err": True})

        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    server = http.server.HTTPServer((HOST, PORT), Handler)
    print(f"shell-ctl on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
