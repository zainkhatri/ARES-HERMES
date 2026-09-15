import os, sys, json, hashlib, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import apply as applymod


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _commands_hash(commands):
    return hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest()


def test_restart_exclude_list_blocks_self_and_core_services():
    for unit in ["ares", "caddy", "ttyd", "pty_ws", "ares-shell-ctl", "ares-autofix-watcher", "ares-autofix-apply"]:
        assert applymod.is_restart_excluded(unit) is True


def test_restart_exclude_list_allows_normal_services():
    assert applymod.is_restart_excluded("ares-facescan") is False


def test_verify_hashes_passes_when_unchanged():
    content = "original file content\n"
    incident = {"diagnosis": {"base_snapshot_hash": _hash(content), "diff": "the diff text", "diff_hash": _hash("the diff text")}}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write(content)
        path = f.name
    try:
        assert applymod.verify_hashes(incident, path) is True
    finally:
        os.unlink(path)


def test_verify_hashes_fails_when_live_file_changed_since_diagnosis():
    incident = {"diagnosis": {"base_snapshot_hash": _hash("original"), "diff": "the diff text", "diff_hash": _hash("the diff text")}}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write("someone edited this after diagnosis")
        path = f.name
    try:
        assert applymod.verify_hashes(incident, path) is False
    finally:
        os.unlink(path)


def test_verify_hashes_fails_when_diff_text_tampered():
    content = "original\n"
    incident = {"diagnosis": {"base_snapshot_hash": _hash(content), "diff": "TAMPERED diff text", "diff_hash": _hash("original diff text")}}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write(content)
        path = f.name
    try:
        assert applymod.verify_hashes(incident, path) is False
    finally:
        os.unlink(path)


def test_apply_refuses_excluded_unit_never_calls_restart():
    content = "x\n"
    incident = {"diagnosis": {"base_snapshot_hash": _hash(content), "diff": "y", "diff_hash": _hash("y")}}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write(content)
        path = f.name
    try:
        calls = []
        status = applymod.apply_and_restart(incident, path, "ares-shell-ctl",
                                             restart_fn=lambda u: calls.append(u), healthcheck_fn=lambda u: True)
        assert status == "revert_failed_needs_human"
        assert calls == []
    finally:
        os.unlink(path)


def test_apply_success_path_marks_resolved():
    content = "original\n"
    diff_text = "thediff"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write(content)
        path = f.name
    try:
        incident = {"diagnosis": {"base_snapshot_hash": _hash(content), "diff": diff_text, "diff_hash": _hash(diff_text)}}
        status = applymod.apply_and_restart(incident, path, "ares-facescan",
                                             restart_fn=lambda u: None, healthcheck_fn=lambda u: True,
                                             patch_fn=lambda p, d, r=False: True)
        assert status == "resolved"
    finally:
        os.unlink(path)


def test_apply_failed_healthcheck_triggers_revert_then_marks_status():
    content = "original\n"
    diff_text = "thediff"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write(content)
        path = f.name
    try:
        incident = {"diagnosis": {"base_snapshot_hash": _hash(content), "diff": diff_text, "diff_hash": _hash(diff_text)}}
        restart_calls = []
        patch_calls = []
        status = applymod.apply_and_restart(
            incident, path, "ares-facescan",
            restart_fn=lambda u: restart_calls.append(u),
            healthcheck_fn=lambda u: False,   # always unhealthy
            patch_fn=lambda p, d, r=False: patch_calls.append(r) or True,
        )
        assert status == "revert_failed_needs_human"
        assert restart_calls == ["ares-facescan", "ares-facescan"]  # applied, then restarted on reverted file
        assert patch_calls == [False, True]  # forward apply, then reverse revert
    finally:
        os.unlink(path)


def test_apply_stale_diff_when_hash_mismatch():
    diff_text = "thediff"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write("live file changed since diagnosis")
        path = f.name
    try:
        incident = {"diagnosis": {"base_snapshot_hash": _hash("different original"), "diff": diff_text, "diff_hash": _hash(diff_text)}}
        status = applymod.apply_and_restart(incident, path, "ares-facescan",
                                             restart_fn=lambda u: None, healthcheck_fn=lambda u: True,
                                             patch_fn=lambda p, d, r=False: True)
        assert status == "stale_diff_needs_human"
    finally:
        os.unlink(path)


def test_apply_patch_failure_leaves_file_untouched():
    content = "original\n"
    diff_text = "thediff"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py") as f:
        f.write(content)
        path = f.name
    try:
        incident = {"diagnosis": {"base_snapshot_hash": _hash(content), "diff": diff_text, "diff_hash": _hash(diff_text)}}
        restart_calls = []
        status = applymod.apply_and_restart(incident, path, "ares-facescan",
                                             restart_fn=lambda u: restart_calls.append(u),
                                             healthcheck_fn=lambda u: True,
                                             patch_fn=lambda p, d, r=False: False)  # patch won't apply
        assert status == "stale_diff_needs_human"
        assert restart_calls == []  # never restarted a unit on an unpatched file
    finally:
        os.unlink(path)


def test_apply_file_only_change_no_unit_skips_healthcheck():
    """A diff with no unit to restart (e.g. a shell script) is done once the
    patch lands -- it must not be spuriously health-checked and reverted."""
    content = "original\n"
    diff_text = "thediff"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh") as f:
        f.write(content)
        path = f.name
    try:
        incident = {"diagnosis": {"base_snapshot_hash": _hash(content), "diff": diff_text, "diff_hash": _hash(diff_text)}}
        restart_calls = []
        status = applymod.apply_and_restart(incident, path, "",
                                             restart_fn=lambda u: restart_calls.append(u),
                                             healthcheck_fn=lambda u: (_ for _ in ()).throw(AssertionError("must not healthcheck a file-only change")),
                                             patch_fn=lambda p, d, r=False: True)
        assert status == "resolved"
        assert restart_calls == []
    finally:
        os.unlink(path)


def test_apply_patch_real_binary_applies_and_reverses(tmp_path):
    """Integration: the default apply_patch actually mutates the file with a
    real unified diff, and reversing restores it byte-for-byte."""
    original = "line one\nERO=root@10.0.1.69\nline three\n"
    f = tmp_path / "script.sh"
    f.write_text(original)
    diff = (
        "--- a/script.sh\n+++ b/script.sh\n"
        "@@ -1,3 +1,3 @@\n line one\n-ERO=root@10.0.1.69\n+ERO=root@100.90.30.81\n line three\n"
    )
    assert applymod.apply_patch(str(f), diff, reverse=False) is True
    assert "100.90.30.81" in f.read_text() and "10.0.1.69" not in f.read_text()
    assert applymod.apply_patch(str(f), diff, reverse=True) is True
    assert f.read_text() == original


def test_run_commands_success():
    commands = ["newaliases", "postqueue -f"]
    incident = {"diagnosis": {"commands": commands, "commands_hash": _commands_hash(commands)}}
    ran = []
    status = applymod.run_commands(incident, "EROS", run_fn=lambda cmd: ran.append(cmd) or True)
    assert status == "resolved"
    assert ran == commands


def test_run_commands_stops_at_first_failure():
    commands = ["cmd1", "cmd2", "cmd3"]
    incident = {"diagnosis": {"commands": commands, "commands_hash": _commands_hash(commands)}}
    ran = []
    def run_fn(cmd):
        ran.append(cmd)
        return cmd != "cmd2"
    status = applymod.run_commands(incident, "ARES", run_fn=run_fn)
    assert status == "command_execution_failed"
    assert ran == ["cmd1", "cmd2"]  # never reaches cmd3


def test_run_commands_fails_closed_on_hash_mismatch():
    commands = ["newaliases"]
    incident = {"diagnosis": {"commands": commands, "commands_hash": "tampered-hash"}}
    ran = []
    status = applymod.run_commands(incident, "EROS", run_fn=lambda cmd: ran.append(cmd) or True)
    assert status == "stale_diff_needs_human"
    assert ran == []


def test_run_commands_denylist_rechecked_at_execution_time():
    """Defense in depth: even if incidents.json somehow ended up with a
    denylisted command post-council (e.g. manual edit), execution still
    re-checks and refuses rather than trusting the stored approval."""
    commands = ["rm -rf /"]
    incident = {"diagnosis": {"commands": commands, "commands_hash": _commands_hash(commands)}}
    ran = []
    status = applymod.run_commands(incident, "ARES", run_fn=lambda cmd: ran.append(cmd) or True)
    assert status == "command_execution_failed"
    assert ran == []


def test_verify_commands_hash_matches():
    commands = ["a", "b"]
    incident = {"diagnosis": {"commands": commands, "commands_hash": _commands_hash(commands)}}
    assert applymod.verify_commands_hash(incident) is True


def test_verify_commands_hash_mismatch():
    incident = {"diagnosis": {"commands": ["a", "b"], "commands_hash": "wrong"}}
    assert applymod.verify_commands_hash(incident) is False


def test_resolve_live_file_path_is_relative_to_repo_root():
    assert applymod.resolve_live_file_path("/repo", "photos/x.py") == "/repo/photos/x.py"
