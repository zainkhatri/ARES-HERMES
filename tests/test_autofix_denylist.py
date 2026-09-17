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
