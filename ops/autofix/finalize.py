"""Consumes the diagnosis a headless Claude session (escalate.sh) produced,
runs it through the mechanical path-denylist gate and a council safety
review, and writes the result back to incident_store -- this is the only
place an incident moves from "escalated" to "council_approved"/"council_held".
Invoked by escalate.sh right after the claude -p session exits."""
import hashlib
import json
import os
import shutil
import sys

import apply
import council
import denylist
import incident_store

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Master switch: NOTHING auto-executes anywhere (ARES included) until this
# file is deliberately created -- explicit user instruction, 2026-09-13:
# "dont execute until i sign off on it". Absent by default. This is separate
# from and on top of the pipeline kill switch (/root/ares-autofix-disabled,
# which stops the whole pipeline including diagnosis); this one specifically
# gates the "skip the human click" behavior. While absent, every finding --
# ARES, EROS, ZEUS alike -- stops at council_approved and waits for the
# dashboard Approve click, i.e. today's known-safe behavior.
SIGNED_OFF_FLAG = "/root/ares-autofix-signed-off"

# Second, even narrower gate on top of the above: once signed off, ARES
# auto-ships immediately (smaller blast radius, only this dashboard/box).
# EROS/ZEUS additionally require THIS flag before ever auto-executing --
# until then, even after sign-off, they still wait for a human Approve
# click on the dashboard.
UNATTENDED_REMOTE_EXEC_FLAG = "/root/ares-autofix-unattended-remote-enabled"


def _signed_off():
    return os.path.exists(SIGNED_OFF_FLAG)


def _unattended_remote_exec_enabled():
    return os.path.exists(UNATTENDED_REMOTE_EXEC_FLAG)


def _auto_apply_if_ares(store, incident_id, box):
    """No auto-execution anywhere until SIGNED_OFF_FLAG is deliberately
    created. Once signed off: an ARES fix auto-ships ONLY on a UNANIMOUS
    council (every member approves) -- any dissent, even 5-of-6, stays at
    council_approved and waits for the dashboard Merge click (user rule,
    2026-09-15). EROS/ZEUS additionally require UNATTENDED_REMOTE_EXEC_FLAG
    on top of that. Returns the final status."""
    if not _signed_off():
        return "council_approved"
    if box != "ARES" and not _unattended_remote_exec_enabled():
        return "council_approved"

    data = store.load()
    incident = next((i for i in data["incidents"] if i["id"] == incident_id), None)
    diag = incident["diagnosis"]

    votes = diag.get("council_votes") or []
    if not votes or not all(v.get("approve") for v in votes):
        return "council_approved"  # not unanimous -> human Merge click required

    if diag.get("commands"):
        result_status = apply.run_commands(incident, box, run_fn=lambda cmd: apply.run_command(box, cmd))
    else:
        live_file_path = apply.resolve_live_file_path(REPO_ROOT, diag.get("target_file", ""))
        result_status = apply.apply_and_restart(
            incident, live_file_path, diag.get("unit_name", ""),
            restart_fn=apply.systemctl_restart, healthcheck_fn=apply.systemctl_healthy,
        )
    store.set_status(incident_id, result_status)
    return result_status


def _archive_log(incident_id, tmp_log_path):
    """Copies the ephemeral /tmp session log to a persisted location so it
    survives a reboot and is fetchable by the dashboard's log viewer."""
    os.makedirs(LOG_DIR, exist_ok=True)
    dest = os.path.join(LOG_DIR, f"{incident_id}.log")
    if os.path.exists(tmp_log_path):
        shutil.copyfile(tmp_log_path, dest)
        return os.path.join("logs", f"{incident_id}.log")
    return None


def _council_review(diff_text, reasoning, target_file):
    """Mandatory post-diagnosis safety gate, now a 4-persona panel. Returns
    the panel dict: {votes, approved, verdict, summary}."""
    context = (
        "<untrusted_diagnosis>\n"
        f"target_file: {target_file}\n"
        f"reasoning: {reasoning}\n"
        f"diff:\n{diff_text}\n"
        "</untrusted_diagnosis>"
    )
    return council.panel_review(context)


def _council_review_recommendation(reasoning, manual_steps, box, commands=None):
    """Same mandatory panel gate, for a finding with no applicable diff
    (remote-host recommendation, or a command-based fix). If `commands` is
    set, the panel is reviewing something that WILL be executed once
    approved -- it must judge the exact commands, not just the prose."""
    executable_note = (
        f"commands (WILL RUN VERBATIM ON {box} IF APPROVED):\n{json.dumps(commands)}\n"
        if commands else
        "commands: none -- this is informational only, nothing will be auto-executed\n"
    )
    context = (
        "<untrusted_recommendation>\n"
        f"box: {box}\n"
        f"reasoning: {reasoning}\n"
        f"manual_steps: {manual_steps}\n"
        f"{executable_note}"
        "</untrusted_recommendation>"
    )
    return council.panel_review(context)


def finalize(incident_id, store_path=None, result_path=None, tmp_log_path=None):
    store = incident_store.IncidentStore(
        store_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "incidents.json")
    )
    result_path = result_path or f"/tmp/ares-autofix-result-{incident_id}.json"
    tmp_log_path = tmp_log_path or f"/tmp/ares-autofix-log-{incident_id}.log"

    log_path = _archive_log(incident_id, tmp_log_path)

    if not os.path.exists(result_path):
        store.write_diagnosis(incident_id, log_path=log_path,
                               fix_title="diagnosis produced no result file",
                               reasoning="the headless session did not write a result -- check the log")
        store.set_status(incident_id, "diagnosis_timeout")
        return "diagnosis_timeout"

    try:
        with open(result_path) as f:
            result = json.load(f)
    except (OSError, ValueError) as e:
        store.write_diagnosis(incident_id, log_path=log_path,
                               fix_title="diagnosis result unreadable",
                               reasoning=f"could not parse result file: {e}")
        store.set_status(incident_id, "diagnosis_timeout")
        return "diagnosis_timeout"

    diff = result.get("diff", "")
    manual_steps = result.get("manual_steps", "")
    commands = result.get("commands", [])
    box = result.get("box", "ARES")
    reasoning = result.get("reasoning", "")
    target_file = result.get("target_file", "")
    fix_title = result.get("fix_title") or (reasoning.split(".")[0][:120] if reasoning else "fix proposed")
    commands_hash = hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest() if commands else ""

    store.write_diagnosis(
        incident_id,
        diff=diff,
        diff_hash=result.get("diff_hash", ""),
        base_snapshot_hash=result.get("base_snapshot_hash", ""),
        target_file=target_file,
        unit_name=result.get("unit_name", ""),
        manual_steps=manual_steps,
        commands=commands,
        commands_hash=commands_hash,
        box=box,
        reasoning=reasoning,
        fix_title=fix_title,
        log_path=log_path,
    )
    store.set_status(incident_id, "diagnosed")

    if not diff:
        # Audit finding with no applicable code change (e.g. a remote-host
        # recommendation on EROS/ZEUS). If it includes `commands`, one human
        # Approve click will execute them for real (same invariant as a
        # diff) -- so the mechanical command-denylist gate applies first,
        # exactly like check_diff_paths gates a diff.
        if not manual_steps and not commands:
            store.write_diagnosis(incident_id, reasoning=reasoning or "no diff, no commands, no manual_steps -- nothing actionable")
            store.set_status(incident_id, "diagnosis_timeout")
            return "diagnosis_timeout"

        if commands:
            is_clean, violations = denylist.check_commands(commands)
            if not is_clean:
                store.write_diagnosis(incident_id, council_verdict=f"blocked: denylisted/destructive command(s): {violations}")
                store.set_status(incident_id, "council_held")
                return "council_held"

        panel = _council_review_recommendation(reasoning, manual_steps, box, commands)
        store.write_diagnosis(incident_id, council_verdict=panel["verdict"],
                               council_votes=panel["votes"], council_discussion=panel.get("discussion", []),
                               council_summary=panel["summary"])
        if not panel["approved"]:
            store.set_status(incident_id, "council_held")
            return "council_held"
        if not commands:
            store.set_status(incident_id, "recommendation_ready")  # prose-only, nothing executable
            return "recommendation_ready"
        store.set_status(incident_id, "council_approved")
        return _auto_apply_if_ares(store, incident_id, box)

    is_clean, violations = denylist.check_diff_paths(diff)
    if not is_clean:
        store.write_diagnosis(incident_id, council_verdict=f"blocked: touches denylisted path(s): {violations}")
        store.set_status(incident_id, "council_held")
        return "council_held"

    panel = _council_review(diff, reasoning, target_file)
    store.write_diagnosis(incident_id, council_verdict=panel["verdict"],
                           council_votes=panel["votes"], council_discussion=panel.get("discussion", []),
                               council_summary=panel["summary"])
    if not panel["approved"]:
        store.set_status(incident_id, "council_held")
        return "council_held"
    store.set_status(incident_id, "council_approved")
    return _auto_apply_if_ares(store, incident_id, box)  # diffs are always within this repo -> always ARES


if __name__ == "__main__":
    incident_id = sys.argv[1]
    print(f"finalize: {finalize(incident_id)}")
