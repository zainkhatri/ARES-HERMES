"""Sole trusted status-writer. Polls the 4 trigger sources, dedups, and
creates new incidents. Runs on the ARES host every ~3 min via
ares-autofix-watcher.timer. Kill-switch check is fail-closed: any error
reading the kill-switch path means "treat as disabled"."""
import json
import os
import subprocess

import dedup
import incident_store
import triage

PENDING_STATUSES = {"new", "escalated", "diagnosed", "council_approved"}
ESCALATE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "escalate.sh")


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


def _launch_escalation(incident_id, signature, source, detail):
    """Non-blocking: escalate.sh itself enforces the daily cap, --max-turns,
    and wall-clock timeout, so it's safe to fire-and-forget here."""
    detail_file = f"/tmp/ares-autofix-detail-{incident_id}.txt"
    with open(detail_file, "w") as f:
        f.write(detail)
    subprocess.Popen(["bash", ESCALATE_SCRIPT, incident_id, signature, source, detail_file])


def _triage_and_route(store, incident_id, signature, source, unit_label, detail):
    """The only place triage.py and escalate.sh get invoked from -- without
    this, watcher.py would only ever create incidents stuck at status=new."""
    result = triage.triage(unit_label, detail[:4000])
    store.write_diagnosis(incident_id, triage_reason=result["reason"])
    if result["escalate"]:
        store.set_status(incident_id, "escalated")
        _launch_escalation(incident_id, signature, source, detail)
    else:
        store.set_status(incident_id, "triaged_skip")


_ERROR_LINE_RE = None  # set below, avoids importing re at module top for one use


def _incident_title(unit_label, excerpt):
    """Human-readable title for the dashboard -- never the raw dedup hash.
    Prefers the most specific error line (e.g. 'ValueError: ...') found in
    the excerpt; falls back to the first non-blank line."""
    import re
    global _ERROR_LINE_RE
    if _ERROR_LINE_RE is None:
        _ERROR_LINE_RE = re.compile(r"^\s*(\w+(?:Error|Exception)):?\s*(.*)$")
    best = None
    for line in excerpt.splitlines():
        m = _ERROR_LINE_RE.match(line)
        if m:
            best = f"{m.group(1)}: {m.group(2)}".strip(": ")
            break
    if not best:
        best = next((l.strip() for l in excerpt.splitlines() if l.strip()), "")
    best = best[:120]
    return f"{unit_label} — {best}" if best else unit_label


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
        detail = excerpt[-4000:]
        iid = store.new_incident(signature, "systemd_failed", detail, title=_incident_title(unit, excerpt))
        _triage_and_route(store, iid, signature, "systemd_failed", unit, detail)
        created += 1

    for job in collect_stale_jobs(host_crons_path):
        unit_label = job.get("unit", job.get("name", "unknown"))
        signature = dedup.normalize_signature(unit_label, "job not ok")
        existing = store.find_by_signature(signature)
        if existing and existing["status"] in PENDING_STATUSES:
            continue
        detail = json.dumps(job)
        title = f"{unit_label} — scheduled job not ok"
        iid = store.new_incident(signature, "dashboard_job", detail, title=title)
        _triage_and_route(store, iid, signature, "dashboard_job", unit_label, detail)
        created += 1

    for alert in collect_alert_files(alert_paths):
        signature = dedup.normalize_signature(alert["path"], alert["content"][:200])
        existing = store.find_by_signature(signature)
        if existing and existing["status"] in PENDING_STATUSES:
            continue
        detail = alert["content"][:4000]
        title = f"{os.path.basename(alert['path'])} — alert"
        iid = store.new_incident(signature, "alert_file", detail, title=title)
        _triage_and_route(store, iid, signature, "alert_file", alert["path"], detail)
        created += 1

    return created


if __name__ == "__main__":
    s = incident_store.IncidentStore("/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ops/autofix/incidents.json")
    n = run_once(s, "/root/ares-autofix-disabled")
    print(f"watcher: {n} new incident(s)")
