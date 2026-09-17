import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import dedup


def test_strips_timestamp():
    a = dedup.normalize_signature("ares-facescan", "Sep 13 03:00:06 pve systemd[1]: Main process exited, code=exited, status=1/FAILURE")
    b = dedup.normalize_signature("ares-facescan", "Sep 12 03:00:08 pve systemd[1]: Main process exited, code=exited, status=1/FAILURE")
    assert a == b


def test_strips_pid():
    a = dedup.normalize_signature("ares-facescan", "Main PID: 3194233 (code=exited, status=1/FAILURE)")
    b = dedup.normalize_signature("ares-facescan", "Main PID: 9981212 (code=exited, status=1/FAILURE)")
    assert a == b


def test_strips_line_numbers_and_memory_addresses():
    a = dedup.normalize_signature("ares-facescan", 'File "ai_indexer.py", line 244, in scan_faces ValueError at 0x7f9a2c001230')
    b = dedup.normalize_signature("ares-facescan", 'File "ai_indexer.py", line 891, in scan_faces ValueError at 0x7f9a2c99abc0')
    assert a == b


def test_different_units_never_collide():
    a = dedup.normalize_signature("ares-facescan", "same text")
    b = dedup.normalize_signature("ares-backup-to-hermes", "same text")
    assert a != b


def test_genuinely_different_errors_do_not_collide():
    a = dedup.normalize_signature("ares-facescan", "ValueError: ambiguous truth value")
    b = dedup.normalize_signature("ares-facescan", "KeyError: 'FACE_EMB_FILE'")
    assert a != b
