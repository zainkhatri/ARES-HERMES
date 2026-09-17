"""Camera-import status file: the single IPC contract between the host importer
(writer) and the Flask dashboard (reader) that drives the "PHOTOS INBOUND" toast.

The file lives in ai_data/ which is bind-mounted into LXC 101, so Flask reads it
directly — same filesystem-IPC pattern the business dashboard already uses. No
socket, no cross-host call. Writes are atomic (temp + os.replace) so a reader
never sees a half-written file.
"""

import json
import os
import tempfile
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_PATH = os.path.join(_APP_DIR, "ai_data", "camera_import_status.json")

# The five states the toast reacts to. Kept in sync with the spec.
ALLOWED_STATES = ("idle", "connected", "draining", "done", "error")


def default_status():
    """Safe idle status used when the file is missing or unreadable."""
    return {
        "state": "idle",
        "batch_total": 0,
        "batch_done": 0,
        "pulled_today": 0,
        "last_update": 0.0,
        "message": "",
    }


def write_status(state, batch_total=0, batch_done=0, pulled_today=0, message=""):
    """Atomically write the status file. Validates every parameter (Power-of-Ten
    rule 7) and returns the written dict."""
    assert state in ALLOWED_STATES, "unknown state: %r" % (state,)
    assert isinstance(batch_total, int) and batch_total >= 0, "bad batch_total"
    assert isinstance(batch_done, int) and 0 <= batch_done <= batch_total, "bad batch_done (must be 0..batch_total)"
    assert isinstance(pulled_today, int) and pulled_today >= 0, "bad pulled_today"

    payload = {
        "state": state,
        "batch_total": batch_total,
        "batch_done": batch_done,
        "pulled_today": pulled_today,
        "last_update": time.time(),
        "message": str(message)[:200],
    }

    d = os.path.dirname(STATUS_PATH)
    assert os.path.isdir(d), "ai_data dir missing: %s" % (d,)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".camera_status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, STATUS_PATH)  # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return payload


def read_status():
    """Read the status file, returning a safe idle default on any error so the
    dashboard never breaks because of a missing/corrupt file."""
    try:
        with open(STATUS_PATH) as f:
            d = json.load(f)
    except Exception:
        return default_status()
    # Validate shape; fall back to default if the state is bogus.
    if not isinstance(d, dict) or d.get("state") not in ALLOWED_STATES:
        return default_status()
    base = default_status()
    base.update({k: d.get(k, base[k]) for k in base})
    return base
