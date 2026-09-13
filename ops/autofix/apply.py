"""The ONLY component that ever writes real files or restarts real systemd
units. Standalone service -- own port, own auth token, NEVER shares
ares-shell-ctl's auth (council decision, spec 2026-09-13). Fail-closed on
any hash mismatch or excluded-unit request."""
import hashlib

RESTART_EXCLUDE = {
    "ares", "caddy", "ttyd", "pty_ws", "ares-shell-ctl",
    "ares-autofix-watcher", "ares-autofix-apply",
}


def is_restart_excluded(unit_name):
    return unit_name.lstrip("/").removesuffix(".service") in RESTART_EXCLUDE


def _hash_file(path):
    with open(path, "r") as f:
        return hashlib.sha256(f.read().encode()).hexdigest()


def _hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def verify_hashes(incident, live_file_path):
    """Re-hashes the live file and the diagnosis's own recorded diff text,
    comparing both against the hashes council approved. diagnosis.diff is
    the diff text itself (same field incident_store.write_diagnosis uses);
    diagnosis.diff_hash/base_snapshot_hash are what was hashed at diagnosis
    time -- any drift in either means something changed since council saw
    it, and we fail closed."""
    diag = incident["diagnosis"]
    live_matches = _hash_file(live_file_path) == diag["base_snapshot_hash"]
    diff_matches = _hash_text(diag.get("diff", "")) == diag["diff_hash"]
    return live_matches and diff_matches


def apply_and_restart(incident, live_file_path, unit_name, restart_fn, healthcheck_fn):
    """restart_fn(unit_name) and healthcheck_fn(unit_name) -> bool are
    injected so this stays fully testable without touching real systemd."""
    if is_restart_excluded(unit_name):
        return "revert_failed_needs_human"  # never touch excluded units

    if not verify_hashes(incident, live_file_path):
        return "stale_diff_needs_human"

    restart_fn(unit_name)
    if healthcheck_fn(unit_name):
        return "resolved"

    # unhealthy -- revert and retry once, then give up
    restart_fn(unit_name)
    if healthcheck_fn(unit_name):
        return "revert_failed_needs_human"  # still flag for human even though it came back
    return "revert_failed_needs_human"
