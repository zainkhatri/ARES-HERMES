"""The ONLY component that ever writes real files, restarts real systemd
units, or executes commands against ARES/EROS/ZEUS. NEVER shares
ares-shell-ctl's auth (council decision, spec 2026-09-13). Fail-closed on
any hash mismatch, excluded-unit request, or command-denylist hit -- the
denylist runs again here, right before execution, not just once at
diagnosis time.

Two callers: finalize.py imports these functions directly to auto-ship a
council_approved incident with no human click (explicit user decision,
2026-09-13 -- "no Zain required", confirmed after being shown the exact
risk: an AI-reviewed command running unattended on EROS, the live
paying-client business box). The standalone HTTP service (__main__ below)
remains as a manual fallback/override path, same auth boundary."""
import hashlib
import json
import os
import subprocess
import time

import denylist


# Repos the autofixer is allowed to write into. ARES-DASHBOARD is the default;
# atlas (the homelab knowledge-graph the council itself queries) is a sibling
# under the same PROJECTS root, so a KG code fix no longer has to fall back to
# a human recommendation just because the file lives one directory over.
_PROJECTS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TARGET_REPOS = {
    "ARES-DASHBOARD": os.path.join(_PROJECTS_ROOT, "ARES-DASHBOARD"),
    "atlas": os.path.join(_PROJECTS_ROOT, "atlas"),
}


def resolve_live_file_path(repo_root, target_file, target_repo=None):
    """Resolves a diagnosis's target file to an absolute path. If target_repo
    is given it must be an allowlisted repo (TARGET_REPOS); otherwise the
    passed repo_root is used. Fails closed (returns None) if the resolved path
    escapes its repo root -- blocks '../' traversal out of the allowlist."""
    root = TARGET_REPOS.get(target_repo, repo_root) if target_repo else repo_root
    full = os.path.realpath(os.path.join(root, target_file))
    if os.path.commonpath([full, os.path.realpath(root)]) != os.path.realpath(root):
        return None  # target escapes its repo -- refuse
    return full


def systemctl_restart(unit_name):
    subprocess.run(["systemctl", "restart", f"{unit_name}.service"], timeout=30)


def systemctl_healthy(unit_name, settle_secs=5):
    time.sleep(settle_secs)
    r = subprocess.run(["systemctl", "is-active", f"{unit_name}.service"],
                        capture_output=True, text=True, timeout=10)
    return r.stdout.strip() == "active"


# Same root-SSH trust the audit session itself uses to investigate EROS/ZEUS
# (bidirectional key trust already in place, per homelab conventions).
SSH_TARGETS = {"EROS": "root@10.0.1.69", "ZEUS": "zeus"}


def run_command(box, cmd, timeout=120):
    try:
        if box == "ARES":
            r = subprocess.run(cmd, shell=True, timeout=timeout)
        else:
            target = SSH_TARGETS.get(box)
            if not target:
                return False
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, cmd],
                timeout=timeout,
            )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False

RESTART_EXCLUDE = {
    "ares", "caddy", "ttyd", "pty_ws", "ares-shell-ctl",
    "ares-autofix-watcher", "ares-autofix-apply", "ares-autofix-audit",
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


def apply_patch(live_file_path, diff_text, reverse=False):
    """Applies (or reverses) a unified diff to a single file via /usr/bin/patch.
    Returns True on a clean apply. The a/ b/ path prefixes in the diff are
    stripped with -p1; the explicit file arg makes patch target that file."""
    assert live_file_path, "live_file_path required"
    args = ["patch", "-p1", "--batch", "--force"]
    if reverse:
        args.append("--reverse")
    args.append(live_file_path)
    try:
        r = subprocess.run(args, input=diff_text, text=True,
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def apply_and_restart(incident, live_file_path, unit_name, restart_fn, healthcheck_fn,
                      patch_fn=apply_patch):
    """Writes the council-approved diff to the live file, then (if there's a
    unit) restarts and health-checks it, reverting on failure. restart_fn,
    healthcheck_fn, and patch_fn are injected so this stays fully testable
    without touching real systemd or files."""
    assert isinstance(incident, dict), "incident must be a dict"
    assert live_file_path, "live_file_path required"
    if is_restart_excluded(unit_name):
        return "revert_failed_needs_human"  # never touch excluded units

    if not verify_hashes(incident, live_file_path):
        return "stale_diff_needs_human"

    diff_text = incident["diagnosis"].get("diff", "")
    if not patch_fn(live_file_path, diff_text, False):
        return "stale_diff_needs_human"  # patch would not apply cleanly; file untouched

    # A file-only change (no unit to restart) is done once the patch lands --
    # there is nothing to health-check, so don't invent a failure (this is the
    # bug that reverted the correct kg-sync-eros fix, 2026-09-15).
    if not unit_name:
        return "resolved"

    restart_fn(unit_name)
    if healthcheck_fn(unit_name):
        return "resolved"

    # unhealthy -- reverse the patch, restart on the known-good file, flag for a human
    patch_fn(live_file_path, diff_text, True)
    restart_fn(unit_name)
    return "revert_failed_needs_human"


def verify_commands_hash(incident):
    diag = incident["diagnosis"]
    commands = diag.get("commands", [])
    return hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest() == diag.get("commands_hash", "")


def run_commands(incident, box, run_fn):
    """run_fn(command: str) -> bool (True = succeeded) is injected so this
    stays fully testable without touching real hosts. Re-checks the command
    denylist immediately before execution -- defense in depth on top of the
    check finalize.py already did at diagnosis time, in case incidents.json
    was edited between council approval and this Approve click. Stops at the
    first failing command; no generic revert exists for arbitrary shell
    commands (unlike a file diff), so a failure is reported honestly, not
    silently retried or rolled back."""
    diag = incident["diagnosis"]
    commands = diag.get("commands", [])

    if not verify_commands_hash(incident):
        return "stale_diff_needs_human"

    is_clean, violations = denylist.check_commands(commands)
    if not is_clean:
        return "command_execution_failed"

    for cmd in commands:
        if not run_fn(cmd):
            return "command_execution_failed"
    return "resolved"


# --- thin HTTP wrapper (host-only service, own port, own auth token) -------
# Glue only: core decision logic above is fully covered by
# tests/test_autofix_apply.py. Not unit-tested further here (ponytail: thin
# wrapper, see plan Task 6 note).
if __name__ == "__main__":
    import os

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

        diag = incident["diagnosis"]
        if diag.get("commands"):
            box = diag.get("box", "ARES")
            result_status = run_commands(incident, box, run_fn=lambda cmd: run_command(box, cmd))
        else:
            unit_name = diag.get("unit_name", "")
            live_file_path = resolve_live_file_path(
                REPO_ROOT, diag.get("target_file", ""), diag.get("target_repo"))
            if live_file_path is None:
                store.set_status(incident_id, "stale_diff_needs_human")
                return jsonify({"error": "target path escapes allowlisted repo"}), 409
            result_status = apply_and_restart(
                incident, live_file_path, unit_name,
                restart_fn=systemctl_restart, healthcheck_fn=systemctl_healthy,
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
