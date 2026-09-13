import os, sys, hashlib, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import apply as applymod


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


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
                                             restart_fn=lambda u: None, healthcheck_fn=lambda u: True)
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
        status = applymod.apply_and_restart(
            incident, path, "ares-facescan",
            restart_fn=lambda u: restart_calls.append(u),
            healthcheck_fn=lambda u: False,   # always unhealthy
        )
        assert status == "revert_failed_needs_human"
        assert restart_calls == ["ares-facescan", "ares-facescan"]  # tried, reverted+retried
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
                                             restart_fn=lambda u: None, healthcheck_fn=lambda u: True)
        assert status == "stale_diff_needs_human"
    finally:
        os.unlink(path)
