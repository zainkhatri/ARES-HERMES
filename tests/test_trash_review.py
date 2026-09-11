import os, json, tempfile
from photos import trash_review as tr

def _seed(trash, name, original, is_photo=True):
    open(os.path.join(trash, name), "w").close()
    meta = {"original_path": original, "trash_name": name,
            "trashed_at": "2026-08-20T20:36:42.449319", "size": 10}
    with open(os.path.join(trash, name + ".meta.json"), "w") as f:
        json.dump(meta, f)

def test_list_photo_trash_filters_non_media():
    trash = tempfile.mkdtemp()
    _seed(trash, "20260820_1_IMG.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/IMG.JPG")
    _seed(trash, "20260820_2_note.txt", "/mnt/data/PROMETHEUS/PHOTOS/x/note.txt")
    items = tr.list_photo_trash(trash)
    names = [i["trash_name"] for i in items]
    assert "20260820_1_IMG.JPG" in names
    assert "20260820_2_note.txt" not in names
    assert items[0]["filename"] == "IMG.JPG"

def test_has_thumb_flag():
    trash = tempfile.mkdtemp(); os.makedirs(os.path.join(trash, "_thumbs"))
    _seed(trash, "20260820_1_IMG.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/IMG.JPG")
    open(os.path.join(trash, "_thumbs", "20260820_1_IMG.JPG.jpg"), "w").close()
    items = tr.list_photo_trash(trash)
    assert items[0]["has_thumb"] is True

def test_trash_thumb_path():
    trash = "/tmp/xbin"
    assert tr.trash_thumb_path(trash, "20260820_1_IMG.JPG") == \
        "/tmp/xbin/_thumbs/20260820_1_IMG.JPG.jpg"

def test_original_file_path():
    trash = "/tmp/xbin"
    assert tr.original_file_path(trash, "20260820_1_IMG.JPG") == \
        "/tmp/xbin/20260820_1_IMG.JPG"

def test_purge_removes_file_meta_and_thumb():
    trash = tempfile.mkdtemp(); os.makedirs(os.path.join(trash, "_thumbs"))
    _seed(trash, "20260820_1_IMG.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/IMG.JPG")
    open(os.path.join(trash, "_thumbs", "20260820_1_IMG.JPG.jpg"), "w").close()
    res = tr.purge_item(trash, "20260820_1_IMG.JPG")
    assert res["success"] is True
    assert not os.path.exists(os.path.join(trash, "20260820_1_IMG.JPG"))
    assert not os.path.exists(os.path.join(trash, "20260820_1_IMG.JPG.meta.json"))
    assert not os.path.exists(os.path.join(trash, "_thumbs", "20260820_1_IMG.JPG.jpg"))

def test_purge_missing_item():
    trash = tempfile.mkdtemp()
    assert tr.purge_item(trash, "nope.jpg")["success"] is False

def test_empty_bin_purges_all_media():
    trash = tempfile.mkdtemp(); os.makedirs(os.path.join(trash, "_thumbs"))
    _seed(trash, "20260820_1_A.JPG", "/mnt/data/PROMETHEUS/PHOTOS/x/A.JPG")
    _seed(trash, "20260820_2_B.PNG", "/mnt/data/PROMETHEUS/PHOTOS/x/B.PNG")
    res = tr.empty_bin(trash)
    assert res["purged"] == 2
    assert tr.list_photo_trash(trash) == []
