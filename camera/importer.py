"""Camera import orchestration: poll loop, drain, reindex.

NetworkManager owns WiFi association. This module is WiFi-agnostic — it
only probes a TCP port and issues HTTP calls; NM handles radio state.

State machine:
  idle → (TCP probe succeeds) → connected → draining → done → idle
  any → (CcapiNotAuthorized) → error
  any → (network error) → error (clears on next successful probe)
"""

import logging
import os
import subprocess
import sys
import tempfile
import time

from camera.ccapi_client import (
    CcapiClient, CcapiError, CcapiNotAuthorized, CcapiNotReachable
)
from camera.ledger import Ledger, MAX_FAILS
from camera.status import read_status, write_status

log = logging.getLogger(__name__)

# Config from environment — read once at module load.
_CAMERA_IP       = os.environ.get("CAMERA_IP", "")
_CAMERA_PORT     = int(os.environ.get("CAMERA_CCAPI_PORT", "8080"))
_POLL_INTERVAL   = int(os.environ.get("CAMERA_POLL_INTERVAL", "10"))
_DEST_ROOT       = os.environ.get("CAMERA_DEST_ROOT",
                                   "/mnt/nvme/PROMETHEUS/PHOTOS")
_PROBE_TIMEOUT   = float(os.environ.get("CAMERA_PROBE_TIMEOUT", "2"))

_APP_DIR         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEDGER_PATH     = os.path.join(_APP_DIR, "ai_data", "camera_import.db")
_SCANNER_SCRIPT  = os.path.join(_APP_DIR, "photo_scanner.py")
_SCANNER_TIMEOUT = 120   # seconds; prevents infinite block on scanner hang


def poll_loop(max_cycles=0):
    """Main entry point. Polls camera and drains when reachable.
    max_cycles=0 runs forever; >0 exits after N cycles (tests only)."""
    assert _CAMERA_IP, "CAMERA_IP env var is required"
    client = CcapiClient(_CAMERA_IP, _CAMERA_PORT, probe_timeout=_PROBE_TIMEOUT)
    ledger = Ledger(_LEDGER_PATH)
    cycle = 0
    try:
        while True:
            if max_cycles and cycle >= max_cycles:  # bounded in tests
                break
            _run_cycle(client, ledger)
            cycle += 1
            time.sleep(_POLL_INTERVAL)
    finally:
        ledger.close()


def _run_cycle(client, ledger):
    """One poll cycle: TCP probe → drain if reachable."""
    assert client is not None, "client required"
    assert ledger is not None, "ledger required"
    if not client.ping():
        return
    try:
        drain(client, ledger)
    except CcapiNotAuthorized as exc:
        log.error("CCAPI_NOT_AUTHORIZED: %s", exc)
        write_status("error", message="CCAPI_NOT_AUTHORIZED")
    except (CcapiNotReachable, CcapiError) as exc:
        log.warning("CCAPI error: %s", exc)
        write_status("error", message=str(exc)[:200])
    except Exception as exc:
        log.exception("unexpected drain error: %s", exc)
        write_status("error", message="internal error")


def drain(client, ledger):
    """List camera contents once, pull all new files, then reindex.
    Content list is snapshotted before the loop — never re-queried mid-drain."""
    assert client is not None, "client required"
    assert ledger is not None, "ledger required"

    prev = read_status()
    pulled_today = int(prev.get("pulled_today", 0))

    write_status("connected", batch_total=0, batch_done=0,
                 pulled_today=pulled_today)

    contents = client.list_contents()   # snapshot ONCE
    new_items = [
        c for c in contents
        if not ledger.is_pulled(c.content_id)
        and not ledger.is_skipped(c.content_id)
    ]

    if not new_items:
        write_status("idle", pulled_today=pulled_today, message="no new photos")
        return

    total = len(new_items)
    write_status("draining", batch_total=total, batch_done=0,
                 pulled_today=pulled_today)

    done = 0
    for item in new_items:              # bounded: len(new_items) is finite
        if _pull_one(client, ledger, item):
            done += 1
            pulled_today += 1
        write_status("draining", batch_total=total, batch_done=done,
                     pulled_today=pulled_today)

    write_status("done", batch_total=total, batch_done=done,
                 pulled_today=pulled_today,
                 message="Added %d photo%s" % (done, "" if done == 1 else "s"))
    if done:
        _reindex()


def _pull_one(client, ledger, item):
    """Download, size-verify, atomically rename one item. Returns True on success."""
    assert client is not None, "client required"
    assert ledger is not None, "ledger required"
    assert item is not None, "item required"
    try:
        dest_dir, dest_name = _resolve_dest(item)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = _collision_safe_path(dest_dir, dest_name)
        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".cam_import.")
        try:
            os.close(fd)
            client.download(item, tmp_path)
            actual = os.path.getsize(tmp_path)
            if item.size and actual != item.size:
                raise ValueError(
                    "size mismatch: got %d expected %d" % (actual, item.size)
                )
            os.replace(tmp_path, dest_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        ledger.mark_pulled(item.content_id, dest_path, actual)
        log.info("pulled %s → %s", item.name, dest_path)
        return True
    except Exception as exc:
        log.warning("failed to pull %s: %s", item.name, exc)
        count = ledger.increment_fail(item.content_id)
        if count >= MAX_FAILS:
            ledger.skip(item.content_id)
            log.error("skipping %s after %d failures (clear with ledger CLI)",
                      item.name, count)
        return False


def _resolve_dest(item):
    """Return (dest_dir, filename) from item's capture_time. Falls back to 'unknown/'."""
    assert item is not None, "item required"
    try:
        # capture_time formats seen: "2026:09:16 14:22:01" or "2026-09-16T14:22:01"
        ct = item.capture_time.replace(":", "-", 2).replace(" ", "T")
        date_part = ct[:10]    # "YYYY-MM-DD"
        year, month = date_part[:4], date_part[5:7]
        assert year.isdigit() and month.isdigit(), "non-numeric year/month"
        return os.path.join(_DEST_ROOT, year, month), item.name
    except Exception:
        return os.path.join(_DEST_ROOT, "unknown"), item.name


def _collision_safe_path(dest_dir, name):
    """Return a path under dest_dir that does not collide with an existing file."""
    assert dest_dir and name, "dest_dir and name required"
    base, ext = os.path.splitext(name)
    candidate = os.path.join(dest_dir, name)
    n = 0
    while os.path.exists(candidate):   # bounded: n < file count in dir
        n += 1
        candidate = os.path.join(dest_dir, "%s_%04d%s" % (base, n, ext))
    return candidate


def _reindex():
    """Run photo_scanner.py --incremental once after a drain. Logs; never raises."""
    if not os.path.isfile(_SCANNER_SCRIPT):
        log.warning("photo_scanner.py not found at %s; skipping reindex",
                    _SCANNER_SCRIPT)
        return
    try:
        result = subprocess.run(
            [sys.executable, _SCANNER_SCRIPT, "--incremental"],
            timeout=_SCANNER_TIMEOUT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("scanner exited %d: %s",
                        result.returncode, result.stderr[:200])
        else:
            log.info("reindex complete")
    except subprocess.TimeoutExpired:
        log.warning("scanner timed out after %ds", _SCANNER_TIMEOUT)
    except Exception as exc:
        log.warning("scanner failed: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    poll_loop()
