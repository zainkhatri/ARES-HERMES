import os, tempfile, shutil
from system import files_api

def _mk_root():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "PHOTOS", "2024"))
    os.makedirs(os.path.join(root, "PHOTOS", ".vault"))
    with open(os.path.join(root, "PHOTOS", "2024", "a.txt"), "w") as f:
        f.write("hi")
    with open(os.path.join(root, "PHOTOS", ".vault", "secret.txt"), "w") as f:
        f.write("nope")
    # legit in-root symlink (allowed), escape symlink (blocked), into-vault symlink (blocked)
    os.symlink(os.path.join(root, "PHOTOS", "2024"), os.path.join(root, "link_ok"))
    os.symlink("/etc", os.path.join(root, "link_escape"))
    os.symlink(os.path.join(root, "PHOTOS", ".vault"), os.path.join(root, "link_vault"))
    return root

def test_safe_resolve_rules():
    root = _mk_root()
    try:
        r = lambda rel: files_api.safe_resolve(rel, root=root)
        # accepts
        assert r("") == os.path.realpath(root)
        assert r("PHOTOS/2024") == os.path.join(os.path.realpath(root), "PHOTOS", "2024")
        assert r("link_ok") == os.path.join(os.path.realpath(root), "PHOTOS", "2024")  # in-root symlink ok
        # rejects
        assert r("../etc/passwd") is None
        assert r("/etc/passwd") is None
        assert r("PHOTOS/../../etc") is None
        assert r("link_escape") is None            # symlink out of root
        assert r("PHOTOS/.vault") is None          # dotfile + deny name
        assert r("link_vault") is None             # symlink resolving into vault
        assert r("PHOTOS/.vault/secret.txt") is None
        assert r("\x00") is None                    # NUL byte
    finally:
        shutil.rmtree(root)
