#!/usr/bin/env python3
"""Dedicated PTY-over-websocket for the A&N native mobile terminal.

The phone shows a swipeable pager of sessions; each page opens ONE connection here and
names the session it wants in its first frame.

Protocol (deliberately trivial so the SwiftTerm client is simple):
  * FIRST text frame  -> JSON control:
      {"list": true}    -> reply {"sessions":[...]} (from `tmux ls`) and close.
      {"open": "NAME"}  -> "shell" = plain `bash -l` (native scrollback); any other name
                           = `tmux new-session -A -s NAME` (a session the Mac/ARES share).
  * binary ws frame   -> raw bytes, written straight to the pty (keyboard input)
  * text ws frame     -> JSON {"cols":N,"rows":M} resize, or {"ctl":"clear"} line-kill
  * pty output        -> sent back as binary ws frames

Tailnet-only (bound to the CGNAT tailscale IP) — same trust boundary as ttyd / ssh ares.
"""
import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import subprocess
import termios

import websockets

HOST = "100.77.42.110"
PORT = 7686
# ctl frames are written straight to the pty. "clear" = C-e (jump to end) + C-u (kill
# line) wipes the current UNSENT input. Scroll is native (SwiftTerm) — no scroll ctl.
CTL_BYTES = {"clear": b"\x05\x15"}


# The "shell" page attaches a persistent, SHARED shell via dtach — a transparent
# pass-through (no tmux copy-mode / full-screen repaint), so SwiftTerm renders it natively
# (smooth scroll) while it survives disconnects and the Mac/PC can attach the SAME shell:
#   ssh ares -t dtach -a /tmp/ares-shell.dtach
# `-r winch` redraws (re-prints the prompt) on attach. tmux targets stay for the Claude TUI.
DTACH_SOCK = "/tmp/ares-shell.dtach"


def _shell_for(target):
    if target == "shell":
        return ["dtach", "-A", DTACH_SOCK, "-r", "winch", "bash", "-l"]
    return ["tmux", "new-session", "-A", "-s", target]


def _valid_target(name):
    # argv (not a shell), but still whitelist to a sane tmux session name.
    return bool(name) and len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9._-]+", name) is not None


def _list_sessions():
    try:
        r = subprocess.run(["tmux", "ls", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    return [n for n in r.stdout.split() if n] if r.returncode == 0 else []


def _windows(session):
    """tmux windows of `session` for the phone's tab strip."""
    assert _valid_target(session), "unvalidated session name"
    try:
        r = subprocess.run(["tmux", "list-windows", "-t", session,
                            "-F", "#{window_index}\t#{window_name}\t#{window_active}"],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines()[:50]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            out.append({"i": int(parts[0]), "name": parts[1][:32], "active": parts[2] == "1"})
    return out


def _window_cmd(action, session, idx):
    assert _valid_target(session), "unvalidated session name"
    idx = max(0, min(999, int(idx)))
    if action == "selectwin":
        return ["tmux", "select-window", "-t", f"{session}:{idx}"]
    if action == "newwin":
        return ["tmux", "new-window", "-t", session]
    if action == "killwin":
        return ["tmux", "kill-window", "-t", f"{session}:{idx}"]
    return None


def _set_winsize(fd, cols, rows):
    assert fd >= 0, "bad pty fd"
    cols = max(1, min(500, int(cols)))
    rows = max(1, min(500, int(rows)))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# Named keys the console may inject — tmux send-keys names, whitelisted.
CONSOLE_KEYS = {"Escape", "Tab", "Enter", "Up", "Down", "Left", "Right",
                "C-c", "C-d", "C-l", "C-r", "C-u", "PPage", "NPage", "BSpace"}


def _capture(session):
    """Pane text incl. ~300 lines of scrollback, SGR colors kept, wrapped lines
    joined (-J) so the phone re-wraps at its own width."""
    assert _valid_target(session), "unvalidated session name"
    try:
        r = subprocess.run(["tmux", "capture-pane", "-p", "-e", "-J", "-S", "-300",
                            "-t", session],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


async def console_loop(ws, target):
    """Command-channel mode: NO pty attach (so the phone never clamps the shared
    window's size). The native console polls `capture` and injects input via
    tmux send-keys — same session the Mac sees, phone-native everything else."""
    last_sent = None
    async for msg in ws:
        if isinstance(msg, (bytes, bytearray)):
            continue
        try:
            d = json.loads(msg)
        except ValueError:
            continue
        c = str(d.get("ctl", ""))
        try:
            if c == "capture":
                txt = _capture(target)
                if txt is not None and txt != last_sent:
                    last_sent = txt
                    await _safe_send(ws, json.dumps({"capture": txt}))
                elif txt is None:
                    await _safe_send(ws, json.dumps({"capture_err": True}))
            elif c == "sendkeys":
                text = str(d.get("text", ""))[:10000]
                if text:
                    subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", text],
                                   capture_output=True, timeout=4)
                if d.get("enter"):
                    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                                   capture_output=True, timeout=4)
            elif c == "key":
                name = str(d.get("name", ""))
                if name in CONSOLE_KEYS:
                    subprocess.run(["tmux", "send-keys", "-t", target, name],
                                   capture_output=True, timeout=4)
            elif c == "windows":
                await _safe_send(ws, json.dumps({"windows": _windows(target)}))
            elif c in ("selectwin", "newwin", "killwin"):
                cmd = _window_cmd(c, target, d.get("i", 0))
                if cmd:
                    subprocess.run(cmd, capture_output=True, timeout=4)
                await _safe_send(ws, json.dumps({"windows": _windows(target)}))
        except (OSError, subprocess.SubprocessError):
            pass


async def handle(ws):
    """One connection = one session. The first frame ({"list"} or {"open"}) decides which."""
    # Wait for the opening control frame.
    try:
        first = await asyncio.wait_for(ws.recv(), timeout=15)
    except (asyncio.TimeoutError, websockets.WebSocketException):
        return
    try:
        d = json.loads(first) if isinstance(first, str) else {}
    except ValueError:
        d = {}

    if d.get("list"):
        await _safe_send(ws, json.dumps({"sessions": _list_sessions()}))
        return

    target = str(d.get("open", "shell"))
    if target != "shell" and not _valid_target(target):
        target = "shell"

    # Console mode: command channel only, no pty fork / tmux attach.
    if d.get("mode") == "console" and target != "shell":
        await console_loop(ws, target)
        return

    shell = _shell_for(target)

    pid, master = pty.fork()
    if pid == 0:                                  # child → become the shell
        os.environ["TERM"] = "xterm-256color"
        os.environ["LANG"] = os.environ.get("LANG", "en_US.UTF-8")
        try:
            os.execvp(shell[0], shell)
        finally:
            os._exit(1)

    loop = asyncio.get_event_loop()
    fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)
    stop = asyncio.Event()

    def on_pty_readable():
        try:
            data = os.read(master, 65536)
        except (OSError, BlockingIOError):
            return
        if data:
            asyncio.create_task(_safe_send(ws, data))
        else:
            stop.set()

    loop.add_reader(master, on_pty_readable)

    async def pump_ws():
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    os.write(master, msg)
                else:
                    try:
                        d = json.loads(msg)
                        if "cols" in d and "rows" in d:
                            _set_winsize(master, d["cols"], d["rows"])
                        elif "ctl" in d:
                            c = str(d["ctl"])
                            b = CTL_BYTES.get(c)
                            if b:
                                os.write(master, b)
                            # Window ops for tmux targets. Replies go back as TEXT
                            # frames — pty output is always binary, so the client can
                            # tell control JSON from terminal bytes by frame type.
                            elif target != "shell" and c == "windows":
                                await _safe_send(ws, json.dumps({"windows": _windows(target)}))
                            elif target != "shell" and c in ("selectwin", "newwin", "killwin"):
                                cmd = _window_cmd(c, target, d.get("i", 0))
                                if cmd:
                                    subprocess.run(cmd, capture_output=True, timeout=4)
                                await _safe_send(ws, json.dumps({"windows": _windows(target)}))
                    except (ValueError, OSError, subprocess.SubprocessError):
                        pass
        finally:
            stop.set()

    pump = asyncio.create_task(pump_ws())
    try:
        await stop.wait()
    finally:
        loop.remove_reader(master)
        pump.cancel()
        for fn in (lambda: os.kill(pid, signal.SIGKILL), lambda: os.close(master)):
            try:
                fn()
            except OSError:
                pass


async def _safe_send(ws, data):
    try:
        await ws.send(data)
    except websockets.WebSocketException:
        pass


async def main():
    async with websockets.serve(handle, HOST, PORT, max_size=None, ping_interval=20):
        print(f"pty-ws on ws://{HOST}:{PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
