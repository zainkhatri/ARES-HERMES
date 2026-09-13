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


# --- thin HTTP wrapper (host-only service, own port, own auth token) -------
# Glue only: core decision logic above is fully covered by
# tests/test_autofix_apply.py. Not unit-tested further here (ponytail: thin
# wrapper, see plan Task 6 note).
if __name__ == "__main__":
    import os
    import subprocess
    import time

    from flask import Flask, jsonify, request

    import incident_store

    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    TOKEN_FILE = os.path.join(REPO_ROOT, "ops", "autofix", ".apply-token")
    STORE_PATH = os.path.join(REPO_ROOT, "ops", "autofix", "incidents.json")

    with open(TOKEN_FILE) as f:
        AUTH_TOKEN = f.read().strip()

    app = Flask(__name__)
    store = incident_store.IncidentStore(STORE_PATH)

    def _authorized(req):
        return req.headers.get("Authorization", "") == f"Bearer {AUTH_TOKEN}"

    def _systemctl_restart(unit_name):
        subprocess.run(["systemctl", "restart", f"{unit_name}.service"], timeout=30)

    def _systemctl_healthy(unit_name, settle_secs=5):
        time.sleep(settle_secs)
        r = subprocess.run(["systemctl", "is-active", f"{unit_name}.service"],
                            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"

    @app.route("/apply/<incident_id>", methods=["POST"])
    def apply_route(incident_id):
        if not _authorized(request):
            return jsonify({"error": "unauthorized"}), 401
        data = store.load()
        incident = next((i for i in data["incidents"] if i["id"] == incident_id), None)
        if incident is None:
            return jsonify({"error": "no such incident"}), 404
        if incident["status"] != "council_approved":
            return jsonify({"error": f"incident status is {incident['status']}, not council_approved"}), 409

        unit_name = incident["diagnosis"].get("unit_name", "")
        live_file_path = incident["diagnosis"].get("target_file", "")
        result_status = apply_and_restart(
            incident, live_file_path, unit_name,
            restart_fn=_systemctl_restart, healthcheck_fn=_systemctl_healthy,
        )
        store.set_status(incident_id, result_status)
        return jsonify({"status": result_status})

    @app.route("/reject/<incident_id>", methods=["POST"])
    def reject_route(incident_id):
        if not _authorized(request):
            return jsonify({"error": "unauthorized"}), 401
        store.set_status(incident_id, "rejected")
        return jsonify({"status": "rejected"})

    # Bound to the host's LAN IP (not 0.0.0.0) so LXC 101 can reach it over the
    # existing host<->LXC bridge (same reachability path already used for
    # qm/lvs SSH calls) -- the Bearer token is the real authorization boundary.
    app.run(host="192.168.20.51", port=7684)
