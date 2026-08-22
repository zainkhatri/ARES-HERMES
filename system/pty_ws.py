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
    # argv (not a shell), but still whitelist. Accepts a bare session name OR a
    # `session:window` pane address (the per-window capture/send target) — the
    # colon form is what _win_target builds; rejecting it here 1011-crashed the
    # connection on every non-active window (blank ssh tab).
    return (bool(name) and len(name) <= 68
            and re.fullmatch(r"[A-Za-z0-9._-]+(:\d+)?", name) is not None)


def _list_sessions():
    try:
        r = subprocess.run(["tmux", "ls", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    return [n for n in r.stdout.split() if n] if r.returncode == 0 else []


# Claude Code's TUI leaves recognizable marks in a captured pane. Any one is
# enough to call a window a Claude session (on either box).
_CLAUDE_SIGNS = ("esc to interrupt", "⏵⏵", "auto-accept edits", "? for shortcuts",
                 "Context left until", "tokens · esc to interrupt")


def _detect_kind(session, idx):
    """Classify window `idx`: ("claude","ares") | ("claude","cronos") | ("shell",None).
    Cheap: one capture + one display-message. Remote Claude is inferred from an
    ssh pane whose content shows the Claude UI."""
    pane = f"{session}:{idx}"
    txt = _capture(pane) or ""
    is_claude = any(s in txt for s in _CLAUDE_SIGNS)
    if not is_claude:
        return "shell", None
    try:
        cmd = subprocess.run(["tmux", "display-message", "-p", "-t", pane,
                             "#{pane_current_command}"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        cmd = ""
    # Claude reached through an ssh pane = the sister box (ZEUS host, ZEUS label).
    return ("claude", "cronos") if cmd == "ssh" else ("claude", "ares")


def _windows(session):
    """tmux windows of `session` for the phone's tab strip, each tagged with the
    kind/src the client renders by."""
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
            idx = int(parts[0])
            kind, src = _detect_kind(session, idx)
            out.append({"i": idx, "name": parts[1][:32], "active": parts[2] == "1",
                        "kind": kind, "src": src})
    return out


_BLOCK_RC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "block-shell.rc")


def _window_cmd(action, session, idx):
    assert _valid_target(session), "unvalidated session name"
    idx = max(0, min(999, int(idx)))
    if action == "selectwin":
        return ["tmux", "select-window", "-t", f"{session}:{idx}"]
    if action == "newwin":
        # Launch with the block-shell rcfile so this window emits OSC 133 markers
        # → the phone renders it as Warp-style command blocks. Scoped to windows
        # the phone opens; the global shell profile is untouched. Named "shell"
        # with auto-rename off so the tab doesn't read "BASH" (or flip names).
        return ["tmux", "new-window", "-t", session, "-n", "shell",
                f"bash --rcfile {_BLOCK_RC} -i",
                ";", "set-option", "-w", "automatic-rename", "off"]
    if action == "killwin":
        return ["tmux", "kill-window", "-t", f"{session}:{idx}"]
    return None


_BLOCK_LOG_DIR = "/tmp/ares-blocks"
_OSC = re.compile(rb"\x1b\]133;([A-D])(?:;(\d+))?\x07")
_ANSI_STRIP_NON_SGR = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z](?<!m)")  # keep SGR (…m), drop the rest
_BRACKET_PASTE = re.compile(rb"\x1b\[\?2004[hl]")


_piped_wids = set()


def _ensure_pipe(session, idx):
    """Start streaming a window's raw output to a per-window log (idempotent).
    pipe-pane taps the pty BEFORE tmux renders it, so OSC 133 markers survive.
    Logs are keyed by tmux's UNIQUE window id (@N, never reused) — keying by
    index made a new tab inherit the block history of a dead tab whose index
    tmux recycled ("+ shows the old ls/^C blocks" bug)."""
    try:
        os.makedirs(_BLOCK_LOG_DIR, exist_ok=True)
    except OSError:
        return None
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", f"{session}:{idx}",
                            "#{window_id}"], capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    wid = r.stdout.strip().lstrip("@")
    log = os.path.join(_BLOCK_LOG_DIR, f"w{wid}.log")
    # pipe-pane once per window: re-issuing on every poll would churn the pipe
    # and can drop output in the gaps.
    if wid not in _piped_wids:
        subprocess.run(["tmux", "pipe-pane", "-t", f"{session}:{idx}",
                        f"cat >> {log}"], capture_output=True, timeout=4)
        _piped_wids.add(wid)
        # Seed a prompt-start marker: the shell's own 133;A for the CURRENT prompt
        # fired before the pipe attached, so without this the first command's echo
        # has no prompt phase and its block loses the command label.
        try:
            with open(log, "ab") as f:
                f.write(b"\x1b]133;A\x07")
        except OSError:
            pass
    return log


def _parse_blocks(raw):
    """Segment a pipe-pane raw byte stream into command blocks using OSC 133.
    A→prompt start, C→output start, D;<ec>→done. Returns newest-last block dicts
    {cmd, out, exit}. Full-screen apps between markers are messy but shells are clean."""
    blocks = []
    # Split on the markers, tracking state. Walk marker by marker.
    pos = 0
    cur = {"cmd": b"", "out": b"", "exit": None, "phase": None}
    segments = []
    last = 0
    for m in _OSC.finditer(raw):
        segments.append(("text", raw[last:m.start()]))
        segments.append(("mark", m.group(1).decode(), m.group(2)))
        last = m.end()
    segments.append(("text", raw[last:]))

    def flush():
        cmd = _clean(cur["cmd"]).strip()
        # Command echo is "❯ <command>" (minimal PS1 from block-shell.rc) — keep
        # only the first line after the prompt glyph.
        cmd = cmd.split("\n", 1)[0]
        if cmd.startswith("❯"):
            cmd = cmd[1:].strip()
        out = _clean(cur["out"]).rstrip()
        # Require actual command/output — drops the spurious empty block from the
        # shell's own startup precmd (which has only an exit code).
        if cmd or out:
            blocks.append({"cmd": cmd, "out": out, "exit": cur["exit"]})

    phase = None
    for seg in segments:
        if seg[0] == "mark":
            code = seg[1]
            if code == "A":
                phase = "prompt"
            elif code == "C":
                phase = "output"
            elif code == "D":
                cur["exit"] = int(seg[2]) if seg[2] else None
                flush()
                cur = {"cmd": b"", "out": b"", "exit": None, "phase": None}
                phase = None
        else:
            txt = seg[1]
            if phase == "prompt":
                cur["cmd"] += txt
            elif phase == "output":
                cur["out"] += txt
    return blocks[-60:]  # cap


def _clean(b):
    """Strip bracketed-paste + non-SGR CSI + the prompt echo cruft; keep SGR color;
    decode. Also drops the leading prompt string up to the last visible ❯/$ on the
    command line."""
    b = _BRACKET_PASTE.sub(b"", b)
    b = _ANSI_STRIP_NON_SGR.sub(b"", b)
    s = b.decode("utf-8", "replace")
    s = s.replace("\r", "")
    return s


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


REPL_CWD = "/mnt/nvme/PROMETHEUS"
CHAT_CWD = "/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD"
CLAUDE_BIN = "/root/.local/bin/claude"


async def repl_loop(ws):
    """Persistent bash over pipes (no pty): each {"run": cmd} streams {"out": ...}
    chunks and ends with {"done": exitcode}. State (cwd, env, vars) persists across
    commands because it's ONE bash. No tty → tools emit plain uncolored text, which
    is exactly what the phone's command cards want."""
    import uuid
    proc = await asyncio.create_subprocess_exec(
        "bash", "--noprofile", "--norc", "-l",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, cwd=REPL_CWD,
        env={**os.environ, "TERM": "dumb", "PS1": ""},
    )
    running = False

    async def stream_until(sentinel):
        nonlocal running
        buf = b""
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                await _safe_send(ws, json.dumps({"done": -1}))
                break
            buf += chunk
            if sentinel in buf:
                head, tail = buf.split(sentinel, 1)
                code = tail.split(b"\n", 1)[0].strip().decode() or "-1"
                if head:
                    await _safe_send(ws, json.dumps({"out": head.decode("utf-8", "replace")}))
                await _safe_send(ws, json.dumps({"done": int(code) if code.lstrip("-").isdigit() else -1}))
                break
            # flush all-but-last-1KB so a partial sentinel isn't split mid-frame
            if len(buf) > 1024:
                flush, buf = buf[:-1024], buf[-1024:]
                await _safe_send(ws, json.dumps({"out": flush.decode("utf-8", "replace")}))
        running = False

    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                d = json.loads(msg)
            except ValueError:
                continue
            cmd = str(d.get("run", ""))[:20000]
            if not cmd:
                continue
            if running:
                await _safe_send(ws, json.dumps({"busy": True}))
                continue
            running = True
            uid = uuid.uuid4().hex[:12]
            sentinel = f"###ARES_EOC:{uid}:".encode()
            proc.stdin.write(cmd.encode() + b"\nprintf '###ARES_EOC:" + uid.encode() + b":%d\\n' $?\n")
            await proc.stdin.drain()
            asyncio.create_task(stream_until(sentinel))
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def chat_loop(ws):
    """Claude Code bridge: each {"prompt": ..., "session": <id|''>} spawns
    `claude -p --output-format stream-json` and forwards every NDJSON event line
    as {"line": <raw json>}. The phone renders structured chat from the events —
    no TUI interpretation anywhere. {"stop": true} kills the active turn."""
    proc = None

    async def run_turn(prompt, session):
        nonlocal proc
        argv = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json",
                "--verbose", "--dangerously-skip-permissions"]
        if session:
            argv += ["--resume", session]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=CHAT_CWD, env={**os.environ, "IS_SANDBOX": "1"},
        )
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                await _safe_send(ws, json.dumps({"line": line.decode("utf-8", "replace").rstrip()}))
        finally:
            rc = await proc.wait()
            await _safe_send(ws, json.dumps({"turn_done": rc}))
            proc = None

    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                d = json.loads(msg)
            except ValueError:
                continue
            if d.get("stop") and proc:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            elif "prompt" in d:
                if proc:
                    await _safe_send(ws, json.dumps({"busy": True}))
                    continue
                prompt = str(d["prompt"])[:50000]
                session = str(d.get("session", ""))[:64]
                if not re.fullmatch(r"[A-Za-z0-9-]*", session):
                    session = ""
                asyncio.create_task(run_turn(prompt, session))
    finally:
        if proc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


import glob as _glob

CLAUDE_PROJECTS = "/root/.claude/projects"


def _newest_transcript():
    """Most-recently-modified Claude Code transcript across all projects = the
    active conversation. Structured JSONL Claude writes itself — NOT TUI scraping."""
    try:
        files = _glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda f: os.path.getmtime(f))


def _find_claude_pid(root_pid):
    """Walk the pane's process tree for the `claude` process (bounded depth)."""
    frontier = [str(root_pid)]
    for _ in range(6):
        if not frontier:
            return None
        nxt = []
        for pid in frontier:
            try:
                with open(f"/proc/{pid}/comm") as f:
                    if f.read().strip() == "claude":
                        return pid
                r = subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True, timeout=3)
                nxt += r.stdout.split()
            except (OSError, subprocess.SubprocessError):
                continue
        frontier = nxt
    return None


def _window_transcript(session, idx):
    """The transcript file of the Claude RUNNING IN THIS WINDOW: pane pid →
    claude process → its cwd → that project's transcript dir → newest .jsonl no
    older than the process. Distinguishes multiple Claudes on the same box —
    without this, every Claude tab showed the globally-newest conversation."""
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", f"{session}:{idx}",
                            "#{pane_pid}"], capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        return None
    cpid = _find_claude_pid(r.stdout.strip())
    if not cpid:
        return None
    try:
        cwd = os.readlink(f"/proc/{cpid}/cwd")
        started = os.stat(f"/proc/{cpid}").st_mtime
    except OSError:
        return None
    proj = os.path.join(CLAUDE_PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd))
    try:
        files = [f for f in _glob.glob(os.path.join(proj, "*.jsonl"))
                 if os.path.getmtime(f) >= started - 5]
    except OSError:
        return None
    return max(files, key=lambda f: os.path.getmtime(f)) if files else None


def _tool_summary(name, inp):
    if not isinstance(inp, dict):
        return ""
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        return os.path.basename(inp.get("file_path", ""))
    if name == "Bash":
        return (inp.get("command", "") or "")[:120]
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    if name == "Task":
        return inp.get("description", "")
    for k in ("query", "url", "prompt", "path"):
        if k in inp:
            return str(inp[k])[:120]
    return ""


ZEUS_SSH = "zain@100.100.29.36"


def _hermes_transcript_bytes():
    """Newest ZEUS Claude transcript over the tailnet. Full-replace model (no
    byte-offset tailing over ssh): return the last ~500 KB, which covers a long
    conversation's recent turns. Bounded, one ssh per poll while the tab is open.
    Returns (bytes, err_string)."""
    try:
        r = subprocess.run(
            ["tailscale", "ssh", ZEUS_SSH,
             "f=$(ls -t ~/.claude/projects/*/*.jsonl 2>/dev/null | head -1); "
             "[ -n \"$f\" ] && tail -c 500000 \"$f\""],
            capture_output=True, timeout=20,
            # systemd services get no $HOME; the tailscale CLI refuses to run
            # without one ("neither $XDG_CONFIG_HOME nor $HOME are defined").
            env={**os.environ, "HOME": os.environ.get("HOME", "/root")})
        if r.returncode != 0:
            return b"", f"rc={r.returncode} {r.stderr[:200].decode('utf-8', 'replace')}"
        return r.stdout, ""
    except (OSError, subprocess.SubprocessError) as e:
        return b"", f"{type(e).__name__}: {e}"


def _parse_transcript_bytes(raw):
    """Parse a JSONL byte blob into compact chat events (drops a leading partial
    line — a mid-file tail may start mid-record)."""
    events = []
    lines = raw.split(b"\n")
    for line in lines[1:] if len(lines) > 1 else lines:
        _append_event(line, events)
    return events


def _parse_transcript(path, off):
    """Parse JSONL from byte offset `off` into compact chat events. Returns
    (events, new_off). Whole-line JSONL, so seeking to a line boundary is safe
    because we always set off to the file size after a full read."""
    events = []
    try:
        with open(path, "rb") as f:
            f.seek(off)
            raw = f.read()
        new_off = off + len(raw)
    except OSError:
        return [], off
    for line in raw.split(b"\n"):
        _append_event(line, events)
    return events, new_off


def _append_event(line, events):
    """Parse one JSONL line and append its chat event(s) to `events`."""
    if not line.strip():
        return
    try:
        d = json.loads(line)
    except ValueError:
        return
    typ = d.get("type")
    msg = d.get("message")
    if typ == "user" and isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            t = c.strip()
            # Skip injected context / command plumbing, keep real prompts.
            if t and not t.startswith(("Caveat:", "<command-", "<local-command",
                                       "<system-reminder", "[Request interrupted")):
                events.append({"role": "user", "text": t[:4000]})
    elif typ == "assistant" and isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, list):
            for b in c:
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    events.append({"role": "assistant", "text": b["text"][:8000]})
                elif bt == "tool_use":
                    events.append({"role": "tool", "name": b.get("name", "tool"),
                                   "detail": _tool_summary(b.get("name", ""), b.get("input"))})


async def console_loop(ws, target):
    """Command-channel mode: NO pty attach (so the phone never clamps the shared
    window's size). The native console polls `capture` and injects input via
    tmux send-keys — same session the Mac sees, phone-native everything else."""
    last_sent = None
    log_state = {}      # per-window transcript tail state: win -> {path, off}

    def _win_target(d):
        """Address a specific window when the client passes `win`, else the
        session's active window."""
        w = d.get("win")
        if isinstance(w, int) and 0 <= w <= 999:
            return f"{target}:{w}"
        return target

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
                txt = _capture(_win_target(d))
                if txt is not None and txt != last_sent:
                    last_sent = txt
                    await _safe_send(ws, json.dumps({"capture": txt}))
                elif txt is None:
                    await _safe_send(ws, json.dumps({"capture_err": True}))
            elif c == "claudelog":
                src = str(d.get("src", "ares"))
                w = d.get("win")
                win = w if isinstance(w, int) and 0 <= w <= 999 else -1
                if src == "cronos":
                    # Full-replace each poll: parse the tail fetched over ssh.
                    raw, err = await asyncio.to_thread(_hermes_transcript_bytes)
                    evs = _parse_transcript_bytes(raw)
                    await _safe_send(ws, json.dumps(
                        {"claudelog": {"reset": True, "events": evs, "off": 0, "err": err,
                                       "src": "cronos", "win": win}}))
                else:
                    # Resolve THIS window's Claude; fall back to global newest when
                    # the window isn't specified (or resolution fails at startup).
                    path = (_window_transcript(target, win) if win >= 0 else None) or \
                           (None if win >= 0 else _newest_transcript())
                    st = log_state.setdefault(win, {"path": None, "off": 0})
                    if path != st["path"]:            # this window's convo (re)started
                        st["path"], st["off"] = path, 0
                        await _safe_send(ws, json.dumps(
                            {"claudelog": {"reset": True, "events": [], "off": 0, "src": "ares", "win": win}}))
                    if path:
                        evs, st["off"] = _parse_transcript(path, st["off"])
                        if evs:
                            await _safe_send(ws, json.dumps(
                                {"claudelog": {"events": evs, "off": st["off"], "src": "ares", "win": win}}))
                    else:
                        # A brand-new Claude session has no transcript file until its
                        # first message. ALWAYS reply — total silence left the phone
                        # spinning forever ("typed claude, never loaded").
                        await _safe_send(ws, json.dumps(
                            {"claudelog": {"waiting": True, "src": "ares", "win": win}}))
            elif c == "blocks":
                w = d.get("win")
                idx = w if isinstance(w, int) and 0 <= w <= 999 else None
                if idx is not None:
                    log = _ensure_pipe(target, idx)
                    raw = b""
                    if log:
                        try:
                            with open(log, "rb") as f:
                                raw = f.read()[-200000:]   # cap tail
                        except OSError:
                            raw = b""
                    await _safe_send(ws, json.dumps({"blocks": _parse_blocks(raw)}))
            elif c == "sendkeys":
                tgt = _win_target(d)
                text = str(d.get("text", ""))[:10000]
                if text:
                    subprocess.run(["tmux", "send-keys", "-t", tgt, "-l", "--", text],
                                   capture_output=True, timeout=4)
                if d.get("enter"):
                    subprocess.run(["tmux", "send-keys", "-t", tgt, "Enter"],
                                   capture_output=True, timeout=4)
            elif c == "key":
                name = str(d.get("name", ""))
                if name in CONSOLE_KEYS:
                    subprocess.run(["tmux", "send-keys", "-t", _win_target(d), name],
                                   capture_output=True, timeout=4)
            elif c == "windows":
                await _safe_send(ws, json.dumps({"windows": _windows(target)}))
            elif c in ("selectwin", "newwin", "killwin"):
                cmd = _window_cmd(c, target, d.get("i", 0))
                if cmd:
                    subprocess.run(cmd, capture_output=True, timeout=4)
                if c == "newwin":
                    # Attach the block pipe at birth — new-window selects the new
                    # window, so the active index is it. Waiting for the first
                    # blocks poll could miss a fast first command.
                    r = subprocess.run(["tmux", "display-message", "-p", "-t", target,
                                        "#{window_index}"], capture_output=True, text=True, timeout=4)
                    if r.returncode == 0 and r.stdout.strip().isdigit():
                        _ensure_pipe(target, int(r.stdout.strip()))
                await _safe_send(ws, json.dumps({"windows": _windows(target)}))
        except (OSError, subprocess.SubprocessError):
            pass


def _origin_ok(ws):
    """CSWSH defense: a browser sends an Origin header — allow only the dashboard's own
    origins (tailnet host / Tailscale IP). Native clients (URLSession) send no Origin →
    allowed. An arbitrary website opening this root PTY is rejected."""
    try:
        origin = ws.request.headers.get("Origin", "") or ""
    except Exception:
        origin = ""
    if not origin:
        return True
    return origin.endswith(".ts.net") or "100.77.42.110" in origin


async def handle(ws):
    """One connection = one session. The first frame ({"list"} or {"open"}) decides which."""
    if not _origin_ok(ws):
        return
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
    if d.get("mode") == "repl":
        await repl_loop(ws)
        return
    if d.get("mode") == "chat":
        await chat_loop(ws)
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
