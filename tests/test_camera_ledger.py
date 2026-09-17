"""Tests for camera.ledger — SQLite dedup ledger."""
import os
import pytest

from camera.ledger import Ledger, MAX_FAILS


@pytest.fixture()
def ledger(tmp_path):
    db = str(tmp_path / "test_ledger.db")
    ld = Ledger(db)
    yield ld
    ld.close()


def test_is_pulled_false_initially(ledger):
    assert ledger.is_pulled("abc123") is False


def test_mark_pulled_and_is_pulled(ledger):
    ledger.mark_pulled("abc123", "/photos/2026/09/IMG_0001.JPG", 1024)
    assert ledger.is_pulled("abc123") is True


def test_mark_pulled_idempotent(ledger):
    ledger.mark_pulled("abc", "/a.jpg", 100)
    ledger.mark_pulled("abc", "/a.jpg", 100)  # must not raise
    assert ledger.is_pulled("abc") is True


def test_different_content_ids_independent(ledger):
    ledger.mark_pulled("id1", "/a.jpg", 10)
    assert ledger.is_pulled("id1") is True
    assert ledger.is_pulled("id2") is False


def test_is_skipped_false_initially(ledger):
    assert ledger.is_skipped("abc") is False


def test_skip_marks_as_skipped(ledger):
    ledger.skip("bad_file")
    assert ledger.is_skipped("bad_file") is True


def test_increment_fail_counts(ledger):
    assert ledger.increment_fail("x") == 1
    assert ledger.increment_fail("x") == 2
    assert ledger.fail_count("x") == 2


def test_fail_count_zero_for_unknown(ledger):
    assert ledger.fail_count("unknown") == 0


def test_max_fails_constant():
    assert MAX_FAILS == 3


def test_pulled_not_skipped(ledger):
    ledger.mark_pulled("ok", "/b.jpg", 50)
    assert ledger.is_pulled("ok") is True
    assert ledger.is_skipped("ok") is False


def test_skip_does_not_affect_pulled(ledger):
    ledger.mark_pulled("done", "/c.jpg", 10)
    ledger.skip("done")   # shouldn't happen in practice, but must not corrupt
    assert ledger.is_pulled("done") is True
