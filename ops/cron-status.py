#!/usr/bin/env python3
"""ARES dashboard: scheduled-job status collector.

Runs on the Proxmox HOST — systemd timers aren't visible inside LXC 101, so the
host writes the status here and system_info.py reads it. Mirrors the host->LXC
bridge that ops/drive-vitals.sh uses for .host_drives.json.

Curated allowlist below: the ARES-relevant jobs only. OS-noise timers (apt,
man-db, logrotate, fstrim, dpkg, tmpfiles, scrubs) are intentionally excluded.
"""
import json
import os
import subprocess
import time

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".host_crons.json")

# systemd timer/unit base name -> friendly label shown on the dashboard.
JOBS = [
    ("ares-backup-to-hermes", "Backup → Zeus"),
    ("ares-facescan",         "Facial Scan"),
    ("ares-elite-picks",      "Elite's Stocks"),
    ("journal-pull",          "Journal pull"),
]


def _us_to_epoch(v):
    """systemd list-timers µs-since-epoch -> int seconds, or None for sentinels."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    # 0 = never; > ~year 2096 in µs = the "infinity" sentinel for next-elapse.
    return v // 1_000_000 if 0 < v < 4_000_000_000_000_000 else None


def _timers():
    """unit-base -> {last, next} epoch seconds, from `systemctl list-timers`."""
    assert isinstance(JOBS, list)
    try:
        r = subprocess.run(["systemctl", "list-timers", "--all", "-o", "json"],
                           capture_output=True, text=True, timeout=8)
        rows = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except Exception:
        rows = []
    out = {}
    for row in rows[:200]:                                  # bounded
        unit = row.get("unit") or ""
        if unit.endswith(".timer"):
            unit = unit[:-6]
        out[unit] = {"last": _us_to_epoch(row.get("last")),
                     "next": _us_to_epoch(row.get("next"))}
    assert isinstance(out, dict)
    return out


def _show(unit, prop):
    """`systemctl show -p PROP --value UNIT`; '' on any failure."""
    assert unit and prop
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p", prop, "--value"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _status(svc):
    """(ok, running) for a oneshot service. Unknown result -> assume ok."""
    assert svc.endswith(".service")
    result = _show(svc, "Result")             # "success" / "exit-code" / "signal" / ...
    active = _show(svc, "ActiveState")        # "inactive" / "active" / "activating"
    running = active in ("active", "activating", "reloading")
    ok = (result == "success") or (result == "" and not running)
    return ok, running


def main():
    timers = _timers()
    jobs = []
    for unit, label in JOBS:                                # bounded
        t = timers.get(unit, {})
        ok, running = _status(unit + ".service")
        jobs.append({"name": label, "unit": unit,
                     "last": t.get("last"), "next": t.get("next"),
                     "ok": bool(ok), "running": bool(running)})
    assert len(jobs) == len(JOBS)
    payload = {"ts": int(time.time()), "jobs": jobs}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
