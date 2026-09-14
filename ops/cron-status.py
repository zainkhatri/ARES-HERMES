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
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".host_cron_logs")

# systemd timer/unit base name -> friendly label shown on the dashboard.
# ares-backup-to-hermes.timer (ARES push to ZEUS) was retired 2026-09-09 in favor
# of zeus-horcrux (ZEUS pulls ARES+EROS at 4am) -- see _zeus_backup_job() below.
JOBS = [
    ("ares-facescan",         "Facial Scan"),
    ("ares-elite-picks",      "Elite's Stocks"),
    ("journal-pull",          "Journal pull"),
    ("ares-autofix-watcher",  "Autofix watcher"),
    ("ares-autofix-audit",    "Autofix audit"),
]

ZEUS_STAMP = "/mnt/nvme/PROMETHEUS/INFRA/status/zeus-backup-stamp"


def _zeus_backup_job():
    """Backup -> Zeus status, from the stamp ZEUS publishes after its nightly
    4am wake->backup->sleep cycle (zeus-horcrux.sh)."""
    import datetime
    last = ok = None
    try:
        with open(ZEUS_STAMP) as f:
            status, ts = f.read().split()[:2]
        last, ok = int(ts), status == "OK"
    except Exception:
        pass
    now = datetime.datetime.now()
    nxt = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=1)
    return {"name": "Backup → Zeus", "unit": "zeus-horcrux",
            "last": last, "next": int(nxt.timestamp()),
            "ok": bool(ok), "running": False}


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


def _write_job_log(unit, lines=300):
    """Persists a bounded journalctl tail per job so the dashboard (LXC,
    no direct journalctl access) can show 'view logs' -- same host-writes,
    LXC-reads bind-mount pattern as .host_crons.json itself."""
    try:
        r = subprocess.run(["journalctl", "-u", f"{unit}.service", "--no-pager", "-n", str(lines)],
                           capture_output=True, text=True, timeout=10)
        content = r.stdout if r.returncode == 0 else ""
    except Exception:
        content = ""
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = os.path.join(LOG_DIR, f"{unit}.log.tmp")
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, os.path.join(LOG_DIR, f"{unit}.log"))


def main():
    timers = _timers()
    jobs = []
    for unit, label in JOBS:                                # bounded
        t = timers.get(unit, {})
        ok, running = _status(unit + ".service")
        jobs.append({"name": label, "unit": unit,
                     "last": t.get("last"), "next": t.get("next"),
                     "ok": bool(ok), "running": bool(running)})
        _write_job_log(unit)
    assert len(jobs) == len(JOBS)
    jobs.insert(0, _zeus_backup_job())
    payload = {"ts": int(time.time()), "jobs": jobs}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
