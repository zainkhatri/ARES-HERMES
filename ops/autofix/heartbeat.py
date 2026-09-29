"""Pipeline heartbeat: each autofix agent (watcher, audit) writes one small
JSON file per run saying whether it ACTUALLY did its job. Motivated by
2026-09-24..28: `claude` left systemd's PATH, every audit session died in
under a second, and audit.py still printed "0 finding(s) processed" and
exited 0 -- "found nothing" and "never looked" were indistinguishable for 5
days. The dashboard reads these files and also flags a heartbeat older
than STALE_SECS, which catches the case no exit code can: the timer never
fired at all.

Detection only. Nothing here repairs PATH, re-enables agents, or retries --
the owner decides that."""
import json
import os
import shutil
import tempfile
import time

HEARTBEAT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heartbeats")
AGENTS = ("watcher", "audit")  # fixed allowlist: agent name becomes a filename
STALE_SECS = 26 * 3600         # both agents run daily; 2h of slack for a slow run


def preflight():
    """Returns None when the claude CLI is callable, else a human-readable
    reason. A clearer failure message, not the detector -- the detector is
    the real session's exit code + result file (see audit.run_once)."""
    path = shutil.which("claude")
    if path is None:
        return f"claude CLI not found on PATH ({os.environ.get('PATH', '')})"
    assert os.path.isabs(path), path
    return None


def _read(agent, hb_dir):
    try:
        with open(os.path.join(hb_dir, f"{agent}.json")) as f:
            data = json.load(f)
    except OSError:
        return None
    except ValueError:
        return {"agent": agent, "ok": False, "ts": None, "last_ok_ts": None,
                "reason": "heartbeat file unreadable (corrupt JSON)", "duration": None}
    return data if isinstance(data, dict) else None


def write(agent, ok, reason, duration, hb_dir=HEARTBEAT_DIR, now=None):
    """Atomic replace (tmp + os.replace) so the dashboard never reads a
    half-written file. last_ok_ts survives failures: it answers 'down since
    when?' without keeping history."""
    if agent not in AGENTS:
        raise ValueError(f"unknown heartbeat agent: {agent!r}")
    assert isinstance(ok, bool), ok
    now = int(time.time()) if now is None else now
    prev = _read(agent, hb_dir) or {}
    record = {"agent": agent, "ts": now, "ok": ok, "reason": str(reason)[:500],
              "duration": duration, "last_ok_ts": now if ok else prev.get("last_ok_ts")}
    os.makedirs(hb_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=hb_dir, prefix=f".{agent}.")
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(hb_dir, f"{agent}.json"))
    return record


def status(hb_dir=HEARTBEAT_DIR, now=None, stale_secs=STALE_SECS):
    """One row per known agent with state ok | failed | stale | missing."""
    now = int(time.time()) if now is None else now
    assert stale_secs > 0, stale_secs
    rows = []
    for agent in AGENTS:
        rec = _read(agent, hb_dir)
        if rec is None:
            rows.append({"agent": agent, "state": "missing", "ts": None, "last_ok_ts": None,
                         "reason": "no heartbeat recorded yet", "duration": None})
            continue
        row = dict(rec, agent=agent)
        if not rec.get("ok"):
            row["state"] = "failed"
        elif not rec.get("ts") or now - rec["ts"] > stale_secs:
            row["state"] = "stale"
            row["reason"] = "no run recorded in over 26h -- timer may not be firing"
        else:
            row["state"] = "ok"
        rows.append(row)
    return rows
