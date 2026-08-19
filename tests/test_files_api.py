import os, tempfile, shutil, pytest
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

def test_kind_for():
    assert files_api.kind_for("a.PDF") == "pdf"
    assert files_api.kind_for("a.jpg") == "image"
    assert files_api.kind_for("a.mp4") == "video"
    assert files_api.kind_for("a.flac") == "audio"
    assert files_api.kind_for("a.md") == "md"
    assert files_api.kind_for("a.py") == "text"
    assert files_api.kind_for("a.bin") == "file"

def test_list_dir_filters_and_sorts():
    root = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(root, "zdir"))
        os.makedirs(os.path.join(root, "adir"))
        os.makedirs(os.path.join(root, ".vault"))       # excluded (deny + dot)
        for n in ("b.txt", "a.txt", ".hidden"):
            open(os.path.join(root, n), "w").close()
        out = files_api.list_dir(root, root=root)
        names = [e["name"] for e in out["entries"]]
        assert names == ["adir", "zdir", "a.txt", "b.txt"]   # dirs first, then name
        assert ".vault" not in names and ".hidden" not in names
        assert out["parent"] is None and out["cwd"] == ""
        assert out["truncated"] is False
        assert out["entries"][2]["kind"] == "text"
    finally:
        shutil.rmtree(root)

def test_serve_mode_policy():
    assert files_api.serve_mode("a.pdf", False) == ("application/pdf", False)
    assert files_api.serve_mode("a.png", False)[1] is False
    assert files_api.serve_mode("a.py", False) == ("text/plain", False)
    assert files_api.serve_mode("evil.html", False) == ("application/octet-stream", True)
    assert files_api.serve_mode("evil.svg", False) == ("application/octet-stream", True)
    assert files_api.serve_mode("a.png", True)[1] is True     # force download

def test_list_dir_truncation():
    root = tempfile.mkdtemp()
    try:
        for i in range(files_api.LIST_CAP + 5):
            open(os.path.join(root, f"f{i}.txt"), "w").close()
        out = files_api.list_dir(root, root=root)
        assert len(out["entries"]) == files_api.LIST_CAP
        assert out["truncated"] is True
    finally:
        shutil.rmtree(root)

def test_open_checked_rejects_symlink():
    d = tempfile.mkdtemp()
    try:
        real = os.path.join(d, "f.txt"); open(real, "w").close()
        link = os.path.join(d, "l.txt"); os.symlink(real, link)
        fd = files_api.open_checked(real); os.close(fd)        # regular file ok
        with pytest.raises(OSError):
            files_api.open_checked(link)                        # symlink rejected
    finally:
        shutil.rmtree(d)
