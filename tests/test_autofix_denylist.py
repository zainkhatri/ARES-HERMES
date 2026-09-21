import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import denylist

CLEAN_DIFF = """--- a/photos/ai_indexer.py
+++ b/photos/ai_indexer.py
@@ -244,1 +244,2 @@
-            existing_face_embs = list(_load_npy_safe(FACE_EMB_FILE) or [])
+            loaded = _load_npy_safe(FACE_EMB_FILE)
+            existing_face_embs = list(loaded) if loaded is not None else []
"""

VAULT_DIFF = """--- a/vault/crypto_core.py
+++ b/vault/crypto_core.py
@@ -1,1 +1,1 @@
-old
+new
"""

PVE_DIFF = """--- a/etc/pve/lxc/101.conf
+++ b/etc/pve/lxc/101.conf
@@ -1,1 +1,1 @@
-old
+new
"""

SELF_MODIFY_DIFF = """--- a/ops/autofix/denylist.py
+++ b/ops/autofix/denylist.py
@@ -1,1 +1,1 @@
-old
+new
"""


def test_clean_diff_passes():
    ok, hits = denylist.check_diff_paths(CLEAN_DIFF)
    assert ok is True
    assert hits == []


def test_vault_path_blocked():
    ok, hits = denylist.check_diff_paths(VAULT_DIFF)
    assert ok is False
    assert any("vault" in h for h in hits)


def test_pve_config_blocked():
    ok, hits = denylist.check_diff_paths(PVE_DIFF)
    assert ok is False
    assert any("etc/pve" in h for h in hits)


def test_autofix_cannot_modify_itself():
    ok, hits = denylist.check_diff_paths(SELF_MODIFY_DIFF)
    assert ok is False
    assert any("ops/autofix" in h for h in hits)


def test_empty_diff_is_clean():
    ok, hits = denylist.check_diff_paths("")
    assert ok is True
    assert hits == []


# --- command gate (host/system fixes) ------------------------------------

def _clean(cmds):
    return denylist.check_commands(cmds)[0]


def test_empty_command_list_is_clean():
    assert denylist.check_commands([]) == (True, [])


def test_benign_host_fix_commands_pass():
    cmds = [
        "sed -i 's/foo/bar/' /etc/systemd/system/caddy.service.d/restart.conf",
        "systemctl daemon-reload",
        "chmod +x /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ops/fleet-collect.sh",
    ]
    assert _clean(cmds) is True


def test_caddy_restart_is_allowed():
    # a deliberate approved fix may restart the ingress; it recovers in ~1s
    assert _clean(["systemctl restart caddy"]) is True


def test_destructive_verbs_are_blocked():
    for cmd in ["rm -rf /var/log", "rm -fr ~/x", "dd if=/dev/zero of=/dev/sda",
                "mkfs.ext4 /dev/sdb1", "reboot", "shutdown -r now",
                "systemctl reboot", "qm stop 200", "pct destroy 110",
                "killall python3", ":(){ :|:& };:"]:
        assert _clean([cmd]) is False, cmd


def test_core_service_takedown_blocked_but_not_restart():
    assert _clean(["systemctl stop ares"]) is False
    assert _clean(["systemctl disable pty_ws"]) is False
    assert _clean(["systemctl mask ares-shell-ctl.service"]) is False


def test_autofix_controlplane_restart_blocked():
    # restarting the running fixer mid-apply interrupts its own run
    assert _clean(["systemctl restart ares-autofix-watcher"]) is False
    assert _clean(["systemctl restart ares-autofix-apply.service"]) is False


def test_denylisted_paths_blocked_in_commands():
    for cmd in ["cat /root/PROMETHEUS/vault/secret", "echo x >> /etc/pve/qemu.conf",
                "touch ops/autofix/incidents.json"]:
        assert _clean([cmd]) is False, cmd


def test_one_bad_command_flags_only_that_command():
    cmds = ["systemctl daemon-reload", "rm -rf /", "systemctl restart caddy"]
    is_clean, violations = denylist.check_commands(cmds)
    assert is_clean is False
    assert violations == ["rm -rf /"]
