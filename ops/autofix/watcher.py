"""Sole trusted status-writer. Polls the 4 trigger sources, dedups, and
creates new incidents. Runs on the ARES host every ~3 min via
ares-autofix-watcher.timer. Kill-switch check is fail-closed: any error
reading the kill-switch path means "treat as disabled"."""
import json
import os
import subprocess

import dedup
import incident_store

PENDING_STATUSES = {"new", "escalated", "diagnosed", "council_approved"}


def _run_systemctl_failed():
    r = subprocess.run(
        ["systemctl", "--failed", "--output", "json"],
        capture_output=True, text=True, timeout=10,
    )
    return r.stdout if r.returncode == 0 else "[]"


def collect_failed_units():
    try:
        return json.loads(_run_systemctl_failed())
    except json.JSONDecodeError:
        return []


def _unit_log_excerpt(unit, lines=30):
    r = subprocess.run(
        ["journalctl", "-u", unit, "--no-pager", "-n", str(lines)],
        capture_output=True, text=True, timeout=10,
    )
    return r.stdout if r.returncode == 0 else ""


def collect_stale_jobs(host_crons_path):
    if not os.path.exists(host_crons_path):
        return []
    try:
        with open(host_crons_path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [j for j in data.get("jobs", []) if not j.get("ok", True)]


def collect_alert_files(alert_paths):
    found = []
    for path in alert_paths:
        if os.path.exists(path):
            with open(path) as f:
                found.append({"path": path, "content": f.read()})
    return found


def _kill_switch_engaged(path):
    """Fail-closed: a genuinely absent file means not-engaged, but any OTHER
    error checking it (permission denied, I/O error, etc.) means 'treat as
    disabled' (return True) -- os.path.exists() swallows those into a plain
    False, which would be fail-OPEN, so we stat directly instead."""
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def run_once(store, kill_switch_path, host_crons_path="/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.host_crons.json",
             alert_paths=("/mnt/nvme/PROMETHEUS/INFRA/status/zeus-ALERT",)):
    if _kill_switch_engaged(kill_switch_path):
        return 0

    created = 0
    for unit_info in collect_failed_units():
        unit = unit_info.get("unit", "")
        if not unit:
            continue
        excerpt = _unit_log_excerpt(unit)
        first_line = next((l for l in excerpt.splitlines() if l.strip()), "")
        signature = dedup.normalize_signature(unit, first_line)
        existing = store.find_by_signature(signature)
        if existing and existing["status"] in PENDING_STATUSES:
            continue
        store.new_incident(signature, "systemd_failed", excerpt[-4000:])
        created += 1

    for job in collect_stale_jobs(host_crons_path):
        signature = dedup.normalize_signature(job.get("unit", job.get("name", "unknown")), "job not ok")
        existing = store.find_by_signature(signature)
        if existing and existing["status"] in PENDING_STATUSES:
            continue
        store.new_incident(signature, "dashboard_job", json.dumps(job))
        created += 1

    for alert in collect_alert_files(alert_paths):
        signature = dedup.normalize_signature(alert["path"], alert["content"][:200])
        existing = store.find_by_signature(signature)
        if existing and existing["status"] in PENDING_STATUSES:
            continue
        store.new_incident(signature, "alert_file", alert["content"][:4000])
        created += 1

    return created


if __name__ == "__main__":
    s = incident_store.IncidentStore("/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ops/autofix/incidents.json")
    n = run_once(s, "/root/ares-autofix-disabled")
    print(f"watcher: {n} new incident(s)")
