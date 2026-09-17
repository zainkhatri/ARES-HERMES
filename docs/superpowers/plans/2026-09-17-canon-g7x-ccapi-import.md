# Canon G7X CCAPI Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the host-side CCAPI transport layer (client, ledger, importer, systemd service) that pulls new JPEGs from a Canon G7X Mark III into `PHOTOS/YYYY/MM/` and drives the existing dashboard toast.

**Architecture:** A host systemd service runs a poll loop that does a fast TCP probe every 10 s; when the camera answers, it lists all content once (snapshot), diffs against a SQLite dedup ledger, streams each new JPEG to a temp file, size-verifies, atomically renames into place, records in the ledger, and runs `photo_scanner.py --incremental` once after the drain. NetworkManager owns WiFi association; the importer is WiFi-agnostic. Status transitions are written to `ai_data/camera_import_status.json` (atomic) so the existing Flask route and dashboard toast need no changes.

**Tech Stack:** Python 3 stdlib + `requests` (already in project), SQLite WAL via `sqlite3`, systemd, NetworkManager (`nmcli`)

## Global Constraints

- Power-of-Ten rules apply: bounded loops, ≤60-line functions, ≥2 assertions per function, check all return values, one level of pointer/indirection, zero warnings.
- Files must stay under 500 lines.
- No dynamic memory allocation after init (no unbounded lists grown in hot loops).
- Every network call has an explicit timeout.
- Dedup key = CCAPI content ID, **not** filename (Canon resets `IMG_0001` after card format).
- Downloads: stream → temp file → size-verify → `os.replace` → ledger write. Scanner only ever sees complete files.
- 401/403 from CCAPI → raise `CcapiNotAuthorized` (named exception, not generic).
- NM profile must set `wifi.powersave=2` and `ipv4.route-metric=50`.
- systemd unit must have `ExecStartPre` interface assertion.
- Three consecutive size-verify failures → skip file with warning log.
- Commit after every task. Commit author: `zainkhatri2560@gmail.com` (not the global work email).

## Already Built — Do Not Touch

| File | What it does |
|---|---|
| `camera/__init__.py` | Package marker |
| `camera/status.py` | Atomic status file IPC (`write_status`, `read_status`) |
| `camera/simulate_drain.py` | Fake drain for dashboard demo |
| `app.py` | Routes `/api/camera/status` + `/api/camera/simulate` already added |
| `templates/home.html` | Toast UI already added |
| `tests/test_camera_status.py` | Status tests passing |

---

## File Structure

| File | Created/Modified | Responsibility |
|---|---|---|
| `camera/ccapi_client.py` | **Create** | Thin CCAPI HTTP wrapper — no orchestration, no FS writes |
| `camera/ledger.py` | **Create** | SQLite dedup ledger with failure counter |
| `camera/importer.py` | **Create** | Poll loop, drain, reindex orchestration |
| `system/ares-camera-import.service` | **Create** | systemd unit with ExecStartPre |
| `system/ares-camera-check-iface` | **Create** | ExecStartPre interface assertion script |
| `system/camera-import.env` | **Create** | Environment variable template |
| `tests/test_camera_ccapi_client.py` | **Create** | Client unit tests (mock HTTP) |
| `tests/test_camera_ledger.py` | **Create** | Ledger unit tests (tmp SQLite) |
| `tests/test_camera_importer.py` | **Create** | Importer drain tests (mock client + tmp dirs) |

---

## Task 1: `camera/ccapi_client.py` — CCAPI HTTP wrapper

**Files:**
- Create: `camera/ccapi_client.py`
- Create: `tests/test_camera_ccapi_client.py`

**Interfaces produced** (Tasks 2–3 depend on these exact names):
- `class CcapiError(Exception)` — base
- `class CcapiNotAuthorized(CcapiError)` — raised on 401/403
- `class CcapiNotReachable(CcapiError)` — raised on TCP/HTTP timeout
- `@dataclass class ContentRef` — fields: `content_id: str`, `name: str`, `size: int`, `capture_time: str`, `url: str`, `file_type: str`
- `class CcapiClient(host, port, probe_timeout, http_timeout, session=None)`
  - `.ping() -> bool`
  - `.api_versions() -> dict`
  - `.list_contents() -> list[ContentRef]`
  - `.file_metadata(item_url: str) -> ContentRef`
  - `.download(ref: ContentRef, dest_path: str) -> int` (bytes written)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_camera_ccapi_client.py
"""Tests for camera.ccapi_client — all HTTP mocked via requests.Session."""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch
import pytest

from camera.ccapi_client import (
    CcapiClient, CcapiError, CcapiNotAuthorized, CcapiNotReachable, ContentRef
)

BASE = "http://192.168.1.1:8080"


def _mock_session(responses):
    """Build a mock requests.Session whose get() returns responses in order."""
    session = MagicMock()
    side_effects = []
    for status, body in responses:
        r = MagicMock()
        r.status_code = status
        r.url = BASE
        if isinstance(body, bytes):
            r.iter_content = lambda chunk_size=65536, b=body: [b]
        else:
            r.json.return_value = body
        side_effects.append(r)
    session.get.side_effect = side_effects
    return session


def _client(session):
    return CcapiClient("192.168.1.1", session=session)


def test_ping_true(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: MagicMock().__enter__())
    assert CcapiClient("192.168.1.1").ping() is True


def test_ping_false(monkeypatch):
    import socket
    def _fail(*a, **kw):
        raise OSError("refused")
    monkeypatch.setattr(socket, "create_connection", _fail)
    assert CcapiClient("192.168.1.1").ping() is False


def test_api_versions_pins_prefix():
    body = {"ccapi": [{"version": "ver100", "apilist": []}]}
    c = _client(_mock_session([(200, body)]))
    c.api_versions()
    assert c._api_prefix == BASE + "/ccapi/ver100"


def test_api_versions_prefers_ver110():
    body = {"ccapi": [{"version": "ver100"}, {"version": "ver110"}]}
    c = _client(_mock_session([(200, body)]))
    c.api_versions()
    assert c._api_prefix == BASE + "/ccapi/ver110"


def test_api_versions_raises_on_empty():
    c = _client(_mock_session([(200, {"ccapi": []})]))
    with pytest.raises(CcapiError):
        c.api_versions()


def test_list_contents_returns_content_refs():
    versions_body = {"ccapi": [{"version": "ver100"}]}
    root_body = {"path": [BASE + "/ccapi/ver100/contents/sd/0/DCIM/100CANON/"]}
    folder_body = {"path": [BASE + "/ccapi/ver100/contents/sd/0/DCIM/100CANON/IMG_0001.JPG"]}
    info_body = {
        "name": "IMG_0001.JPG", "size": 1024, "format": "jpeg",
        "datetime_original": "2026:09:16 14:22:01", "id": "abc123"
    }
    s = _mock_session([(200, versions_body), (200, root_body),
                       (200, folder_body), (200, info_body)])
    c = _client(s)
    refs = c.list_contents()
    assert len(refs) == 1
    r = refs[0]
    assert isinstance(r, ContentRef)
    assert r.content_id == "abc123"
    assert r.name == "IMG_0001.JPG"
    assert r.size == 1024
    assert r.file_type == "jpeg"


def test_list_contents_skips_non_jpeg():
    versions_body = {"ccapi": [{"version": "ver100"}]}
    root_body = {"path": [BASE + "/ccapi/ver100/contents/sd/0/DCIM/100CANON/"]}
    folder_body = {"path": [
        BASE + "/ccapi/ver100/contents/sd/0/DCIM/100CANON/IMG_0001.CR3",
        BASE + "/ccapi/ver100/contents/sd/0/DCIM/100CANON/IMG_0002.JPG",
    ]}
    info_body = {"name": "IMG_0002.JPG", "size": 512, "format": "jpeg",
                 "datetime_original": "2026:09:16 15:00:00", "id": "def456"}
    s = _mock_session([(200, versions_body), (200, root_body),
                       (200, folder_body), (200, info_body)])
    c = _client(s)
    refs = c.list_contents()
    assert len(refs) == 1
    assert refs[0].name == "IMG_0002.JPG"


def test_raises_not_authorized_on_401():
    s = _mock_session([(401, {})])
    c = _client(s)
    with pytest.raises(CcapiNotAuthorized):
        c.api_versions()


def test_raises_not_authorized_on_403():
    s = _mock_session([(403, {})])
    c = _client(s)
    with pytest.raises(CcapiNotAuthorized):
        c.api_versions()


def test_raises_ccapi_error_on_500():
    s = _mock_session([(500, {})])
    c = _client(s)
    with pytest.raises(CcapiError):
        c.api_versions()


def test_download_writes_bytes_and_returns_count():
    ref = ContentRef(
        content_id="x1", name="IMG_0001.JPG", size=6,
        capture_time="2026:09:16 14:00:00",
        url=BASE + "/ccapi/ver100/contents/sd/0/DCIM/100CANON/IMG_0001.JPG",
        file_type="jpeg",
    )
    payload = b"HELLO!"
    session = MagicMock()
    r = MagicMock()
    r.status_code = 200
    r.url = ref.url
    r.iter_content.return_value = [payload]
    session.get.return_value = r
    c = _client(session)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        tmp = f.name
    try:
        n = c.download(ref, tmp)
        assert n == 6
        assert open(tmp, "rb").read() == b"HELLO!"
    finally:
        os.unlink(tmp)


def test_download_raises_not_authorized():
    ref = ContentRef("x", "f.jpg", 0, "", BASE + "/f.jpg", "jpeg")
    session = MagicMock()
    r = MagicMock()
    r.status_code = 401
    r.url = ref.url
    session.get.return_value = r
    c = _client(session)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        tmp = f.name
    try:
        with pytest.raises(CcapiNotAuthorized):
            c.download(ref, tmp)
    finally:
        os.unlink(tmp)
```

- [ ] **Step 2: Run tests — confirm they all fail**

```bash
cd /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
python3 -m pytest tests/test_camera_ccapi_client.py -q 2>&1 | head -20
```

Expected: `ImportError` or `ModuleNotFoundError` for `camera.ccapi_client`.

- [ ] **Step 3: Implement `camera/ccapi_client.py`**

```python
# camera/ccapi_client.py
"""Thin Canon CCAPI HTTP wrapper.

No orchestration, no filesystem writes. Every public method raises on error;
callers handle exceptions. 401/403 → CcapiNotAuthorized (named, not generic).
"""

import socket
from dataclasses import dataclass

import requests


class CcapiError(Exception):
    """Base class for CCAPI errors."""


class CcapiNotAuthorized(CcapiError):
    """CCAPI returned 401 or 403. Camera may require a pairing handshake."""


class CcapiNotReachable(CcapiError):
    """TCP connect or HTTP call timed out or refused."""


@dataclass
class ContentRef:
    content_id: str   # CCAPI stable ID used as dedup key; falls back to URL path
    name: str         # filename on card (e.g. IMG_0001.JPG)
    size: int         # bytes per CCAPI metadata
    capture_time: str # ISO-ish datetime string (e.g. "2026:09:16 14:22:01")
    url: str          # full CCAPI download URL
    file_type: str    # "jpeg", "cr3", etc. (lowercased)


class CcapiClient:
    """Canon CCAPI client bound to one camera host:port."""

    _CHUNK = 65536  # download chunk size — bounded buffer (Power-of-Ten rule 3)

    def __init__(self, host, port=8080, probe_timeout=2.0,
                 http_timeout=30.0, session=None):
        assert host, "host required"
        assert 1 <= int(port) <= 65535, "bad port"
        assert probe_timeout > 0, "probe_timeout must be positive"
        assert http_timeout > 0, "http_timeout must be positive"
        self._host = host
        self._port = int(port)
        self._probe_timeout = probe_timeout
        self._http_timeout = http_timeout
        self._base = "http://%s:%d" % (host, self._port)
        self._api_prefix = None  # set by api_versions()
        self._session = session or requests.Session()

    def ping(self):
        """Fast TCP connect only. True = port open.
        Does NOT confirm CCAPI is authorized or in transfer-ready mode."""
        try:
            with socket.create_connection(
                (self._host, self._port), timeout=self._probe_timeout
            ):
                return True
        except OSError:
            return False

    def api_versions(self):
        """GET /ccapi/ → dict. Pins _api_prefix to the best available version."""
        r = self._get(self._base + "/ccapi/")
        d = r.json()
        versions = [e.get("version", "") for e in d.get("ccapi", [])]
        assert versions, "no versions in /ccapi/ response"
        for pref in ("ver110", "ver100"):
            if pref in versions:
                self._api_prefix = "%s/ccapi/%s" % (self._base, pref)
                return d
        self._api_prefix = "%s/ccapi/%s" % (self._base, versions[-1])
        return d

    def list_contents(self):
        """Enumerate JPEG files on the SD card. Returns list[ContentRef].
        MUST be called once and snapshotted by the caller before drain starts."""
        if self._api_prefix is None:
            self.api_versions()
        r = self._get(self._api_prefix + "/contents/sd/0")
        folders = r.json().get("path", [])
        refs = []
        for folder_url in folders:           # bounded: folder count is finite
            if not isinstance(folder_url, str):
                continue
            refs.extend(self._list_folder(folder_url))
        return refs

    def _list_folder(self, folder_url):
        """List one DCIM folder, returning ContentRef for each JPEG."""
        assert isinstance(folder_url, str), "folder_url must be str"
        r = self._get(folder_url)
        items = r.json().get("path", [])
        refs = []
        for item_url in items:               # bounded: items per folder is finite
            if not isinstance(item_url, str):
                continue
            if not item_url.lower().endswith(".jpg"):
                continue
            try:
                refs.append(self.file_metadata(item_url))
            except CcapiError:
                pass  # skip unreadable metadata; log at caller
        return refs

    def file_metadata(self, item_url):
        """GET <item_url>/info → ContentRef."""
        assert isinstance(item_url, str) and item_url, "item_url required"
        r = self._get(item_url + "/info")
        d = r.json()
        name = d.get("name") or item_url.split("/")[-1]
        size = int(d.get("size", 0))
        ct = d.get("datetime_original") or d.get("datetime") or ""
        ftype = (d.get("format") or "jpeg").lower()
        cid = d.get("id") or item_url      # prefer stable CCAPI ID
        assert isinstance(name, str) and name, "bad name in metadata"
        assert size >= 0, "bad size in metadata"
        return ContentRef(
            content_id=cid, name=name, size=size,
            capture_time=ct, url=item_url, file_type=ftype,
        )

    def download(self, ref, dest_path):
        """Stream ref.url to dest_path. Returns bytes written.
        Caller must size-verify and atomically rename into the final location."""
        assert isinstance(ref, ContentRef), "ref must be ContentRef"
        assert dest_path, "dest_path required"
        r = self._session.get(ref.url, timeout=self._http_timeout, stream=True)
        self._check_status(r)
        written = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=self._CHUNK):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
        assert written >= 0, "written must be non-negative"
        return written

    # ------------------------------------------------------------------ #
    # internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _get(self, url):
        """GET url with timeout; raise on auth failure or non-200."""
        assert url, "url required"
        try:
            r = self._session.get(url, timeout=self._http_timeout)
        except requests.exceptions.Timeout:
            raise CcapiNotReachable("timeout: %s" % url)
        except requests.exceptions.ConnectionError as e:
            raise CcapiNotReachable("connection error: %s" % e)
        self._check_status(r)
        return r

    def _check_status(self, r):
        assert r is not None, "response required"
        if r.status_code in (401, 403):
            raise CcapiNotAuthorized("HTTP %d from %s" % (r.status_code, r.url))
        if r.status_code != 200:
            raise CcapiError("HTTP %d from %s" % (r.status_code, r.url))
```

- [ ] **Step 4: Run tests — all must pass**

```bash
python3 -m pytest tests/test_camera_ccapi_client.py -q
```

Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
git add camera/ccapi_client.py tests/test_camera_ccapi_client.py
git commit -m "feat: add CCAPI HTTP client with CcapiNotAuthorized and ContentRef"
```

---

## Task 2: `camera/ledger.py` — dedup ledger

**Files:**
- Create: `camera/ledger.py`
- Create: `tests/test_camera_ledger.py`

**Interfaces consumed:**
- None (standalone SQLite module)

**Interfaces produced** (Task 3 depends on these):
- `MAX_FAILS: int = 3`
- `class Ledger(db_path: str)`
  - `.is_pulled(content_id: str) -> bool`
  - `.mark_pulled(content_id: str, dest_path: str, size: int, ts: float = None)`
  - `.is_skipped(content_id: str) -> bool`
  - `.increment_fail(content_id: str) -> int` (returns new count)
  - `.fail_count(content_id: str) -> int`
  - `.skip(content_id: str)`
  - `.close()`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_camera_ledger.py
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
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python3 -m pytest tests/test_camera_ledger.py -q 2>&1 | head -10
```

Expected: `ImportError` for `camera.ledger`.

- [ ] **Step 3: Implement `camera/ledger.py`**

```python
# camera/ledger.py
"""Camera import dedup ledger.

Dedup key: CCAPI content ID — NOT filename. Canon resets IMG_0001 after card
format; filename-only dedup silently skips re-imported photos.

Three consecutive size-verify failures → file is skipped and excluded from
future drains until manually cleared (ledger.skip). Prevents corrupt-on-camera
files from spinning the poll loop indefinitely.
"""

import sqlite3
import time

MAX_FAILS = 3  # strike limit before a file is permanently skipped

_SQL_PULLS = """
CREATE TABLE IF NOT EXISTS pulls (
    content_id  TEXT    PRIMARY KEY,
    dest_path   TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    pulled_at   REAL    NOT NULL
)
"""

_SQL_FAILURES = """
CREATE TABLE IF NOT EXISTS failures (
    content_id  TEXT    PRIMARY KEY,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0
)
"""


class Ledger:
    """Thread-safe (WAL mode) SQLite ledger for camera import dedup."""

    def __init__(self, db_path):
        assert db_path, "db_path required"
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SQL_PULLS)
        self._conn.execute(_SQL_FAILURES)
        self._conn.commit()

    def is_pulled(self, content_id):
        """True if content_id has been successfully pulled and verified."""
        assert content_id, "content_id required"
        row = self._conn.execute(
            "SELECT 1 FROM pulls WHERE content_id = ?", (content_id,)
        ).fetchone()
        return row is not None

    def mark_pulled(self, content_id, dest_path, size, ts=None):
        """Record a successfully size-verified pull. Idempotent."""
        assert content_id, "content_id required"
        assert dest_path, "dest_path required"
        assert isinstance(size, int) and size >= 0, "size must be non-negative int"
        if ts is None:
            ts = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO pulls (content_id, dest_path, size, pulled_at)"
            " VALUES (?, ?, ?, ?)",
            (content_id, dest_path, size, float(ts)),
        )
        self._conn.commit()

    def increment_fail(self, content_id):
        """Record one failed size-verify. Returns new consecutive fail count."""
        assert content_id, "content_id required"
        self._conn.execute(
            "INSERT INTO failures (content_id, fail_count, skipped) VALUES (?, 1, 0)"
            " ON CONFLICT(content_id) DO UPDATE SET fail_count = fail_count + 1",
            (content_id,),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT fail_count FROM failures WHERE content_id = ?", (content_id,)
        ).fetchone()
        assert row is not None, "failure row must exist after insert"
        return row[0]

    def fail_count(self, content_id):
        """Current consecutive-failure count (0 if never failed)."""
        assert content_id, "content_id required"
        row = self._conn.execute(
            "SELECT fail_count FROM failures WHERE content_id = ?", (content_id,)
        ).fetchone()
        return row[0] if row else 0

    def skip(self, content_id):
        """Permanently skip content_id. Excluded from future drains."""
        assert content_id, "content_id required"
        self._conn.execute(
            "INSERT INTO failures (content_id, fail_count, skipped) VALUES (?, 0, 1)"
            " ON CONFLICT(content_id) DO UPDATE SET skipped = 1",
            (content_id,),
        )
        self._conn.commit()

    def is_skipped(self, content_id):
        """True if content_id has been permanently skipped."""
        assert content_id, "content_id required"
        row = self._conn.execute(
            "SELECT skipped FROM failures WHERE content_id = ?", (content_id,)
        ).fetchone()
        return bool(row and row[0])

    def close(self):
        """Close the database connection."""
        self._conn.close()
```

- [ ] **Step 4: Run tests — all must pass**

```bash
python3 -m pytest tests/test_camera_ledger.py -q
```

Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
git add camera/ledger.py tests/test_camera_ledger.py
git commit -m "feat: add camera import dedup ledger with CCAPI content ID key"
```

---

## Task 3: `camera/importer.py` — poll loop and drain

**Files:**
- Create: `camera/importer.py`
- Create: `tests/test_camera_importer.py`

**Interfaces consumed:**
- `camera.ccapi_client`: `CcapiClient`, `CcapiNotAuthorized`, `CcapiNotReachable`, `CcapiError`, `ContentRef`
- `camera.ledger`: `Ledger`, `MAX_FAILS`
- `camera.status`: `write_status`, `read_status`

**Interfaces produced:**
- `poll_loop(max_cycles: int = 0)` — entry point for systemd service (`python3 -m camera.importer`)
- `drain(client: CcapiClient, ledger: Ledger)` — exported for integration testing

- [ ] **Step 1: Write failing tests**

```python
# tests/test_camera_importer.py
"""Tests for camera.importer — drain logic with fake client and tmp PHOTOS dir."""
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from camera.ccapi_client import ContentRef, CcapiNotAuthorized, CcapiNotReachable
from camera.ledger import Ledger, MAX_FAILS


def _make_ref(content_id, name, size, capture_time="2026:09:16 14:22:01"):
    return ContentRef(
        content_id=content_id,
        name=name,
        size=size,
        capture_time=capture_time,
        url="http://192.168.1.1:8080/ccapi/ver100/contents/sd/0/DCIM/100CANON/" + name,
        file_type="jpeg",
    )


def _fake_download(ref, dest_path):
    """Write ref.size bytes of zeros to dest_path."""
    with open(dest_path, "wb") as f:
        f.write(b"\x00" * ref.size)
    return ref.size


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Yields (client_mock, ledger, photos_root)."""
    photos = str(tmp_path / "PHOTOS")
    os.makedirs(photos)
    db = str(tmp_path / "ledger.db")
    ld = Ledger(db)
    monkeypatch.setenv("CAMERA_DEST_ROOT", photos)
    monkeypatch.setenv("CAMERA_IP", "192.168.1.1")

    client = MagicMock()
    client.ping.return_value = True
    client.download.side_effect = _fake_download

    yield client, ld, photos
    ld.close()


def test_drain_pulls_new_files(setup, monkeypatch):
    client, ledger, photos = setup
    refs = [_make_ref("id1", "IMG_0001.JPG", 10)]
    client.list_contents.return_value = refs

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert ledger.is_pulled("id1")
    pulled = [f for f in os.listdir(os.path.join(photos, "2026", "09"))
              if f.endswith(".JPG")]
    assert len(pulled) == 1


def test_drain_skips_already_pulled(setup):
    client, ledger, photos = setup
    refs = [_make_ref("id1", "IMG_0001.JPG", 10)]
    client.list_contents.return_value = refs
    ledger.mark_pulled("id1", "/already.jpg", 10)

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    client.download.assert_not_called()


def test_drain_skips_already_skipped(setup):
    client, ledger, photos = setup
    refs = [_make_ref("bad", "IMG_0001.JPG", 10)]
    client.list_contents.return_value = refs
    ledger.skip("bad")

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    client.download.assert_not_called()


def test_drain_per_file_isolation(setup):
    """A failure on file 1 must not prevent file 2 from being pulled."""
    client, ledger, photos = setup
    refs = [
        _make_ref("bad", "IMG_0001.JPG", 99),
        _make_ref("good", "IMG_0002.JPG", 5),
    ]
    client.list_contents.return_value = refs

    call_count = [0]
    def selective_download(ref, dest_path):
        call_count[0] += 1
        if ref.content_id == "bad":
            raise OSError("connection reset")
        return _fake_download(ref, dest_path)

    client.download.side_effect = selective_download

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert not ledger.is_pulled("bad")
    assert ledger.is_pulled("good")


def test_drain_size_mismatch_increments_fail(setup):
    client, ledger, photos = setup
    refs = [_make_ref("truncated", "IMG_0001.JPG", 100)]
    client.list_contents.return_value = refs

    def short_download(ref, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"\x00" * 10)  # only 10 bytes, expect 100
        return 10

    client.download.side_effect = short_download

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert ledger.fail_count("truncated") == 1
    assert not ledger.is_pulled("truncated")


def test_drain_three_failures_skips_file(setup):
    """After MAX_FAILS failures, file is permanently skipped."""
    client, ledger, photos = setup
    refs = [_make_ref("corrupt", "IMG_0001.JPG", 100)]
    client.list_contents.return_value = refs

    def always_short(ref, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"\x00")
        return 1

    client.download.side_effect = always_short

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        for _ in range(MAX_FAILS):
            importer.drain(client, ledger)

    assert ledger.is_skipped("corrupt")


def test_drain_collision_safe_naming(setup):
    """Two files with the same name get unique paths."""
    client, ledger, photos = setup
    ref1 = _make_ref("id1", "IMG_0001.JPG", 4)
    ref2 = _make_ref("id2", "IMG_0001.JPG", 4)
    client.list_contents.return_value = [ref1]

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    client.list_contents.return_value = [ref2]
    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        importer.drain(client, ledger)

    dir_path = os.path.join(photos, "2026", "09")
    files = sorted(os.listdir(dir_path))
    assert len(files) == 2
    assert files[0] != files[1]


def test_drain_unknown_date_fallback(setup):
    """Bad capture_time lands in 'unknown/' not crashes."""
    client, ledger, photos = setup
    ref = _make_ref("id1", "IMG_0001.JPG", 3, capture_time="BADDATE")
    client.list_contents.return_value = [ref]

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex"):
        from camera import importer
        importer.drain(client, ledger)

    assert ledger.is_pulled("id1")
    assert os.path.isfile(os.path.join(photos, "unknown", "IMG_0001.JPG"))


def test_reindex_not_called_on_empty_drain(setup):
    client, ledger, photos = setup
    client.list_contents.return_value = []

    with patch("camera.importer._DEST_ROOT", photos), \
         patch("camera.importer._reindex") as mock_reindex:
        from camera import importer
        importer.drain(client, ledger)

    mock_reindex.assert_not_called()
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
python3 -m pytest tests/test_camera_importer.py -q 2>&1 | head -10
```

Expected: `ImportError` for `camera.importer`.

- [ ] **Step 3: Implement `camera/importer.py`**

```python
# camera/importer.py
"""Camera import orchestration: poll loop, drain, reindex.

NetworkManager owns WiFi association. This module is WiFi-agnostic — it
only probes a TCP port and issues HTTP calls; NM handles radio state.

State machine:
  idle → (TCP probe succeeds) → connected → draining → done → idle
  any → (CcapiNotAuthorized) → error
  any → (network error) → error (clears on next successful probe)
"""

import logging
import os
import subprocess
import sys
import tempfile
import time

from camera.ccapi_client import (
    CcapiClient, CcapiError, CcapiNotAuthorized, CcapiNotReachable
)
from camera.ledger import Ledger, MAX_FAILS
from camera.status import read_status, write_status

log = logging.getLogger(__name__)

# Config from environment — read once at module load.
_CAMERA_IP       = os.environ.get("CAMERA_IP", "")
_CAMERA_PORT     = int(os.environ.get("CAMERA_CCAPI_PORT", "8080"))
_POLL_INTERVAL   = int(os.environ.get("CAMERA_POLL_INTERVAL", "10"))
_DEST_ROOT       = os.environ.get("CAMERA_DEST_ROOT",
                                   "/mnt/nvme/PROMETHEUS/PHOTOS")
_PROBE_TIMEOUT   = float(os.environ.get("CAMERA_PROBE_TIMEOUT", "2"))

_APP_DIR         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEDGER_PATH     = os.path.join(_APP_DIR, "ai_data", "camera_import.db")
_SCANNER_SCRIPT  = os.path.join(_APP_DIR, "photo_scanner.py")
_SCANNER_TIMEOUT = 120   # seconds; prevents infinite block on scanner hang


def poll_loop(max_cycles=0):
    """Main entry point. Polls camera and drains when reachable.
    max_cycles=0 runs forever; >0 exits after N cycles (tests only)."""
    assert _CAMERA_IP, "CAMERA_IP env var is required"
    client = CcapiClient(_CAMERA_IP, _CAMERA_PORT, probe_timeout=_PROBE_TIMEOUT)
    ledger = Ledger(_LEDGER_PATH)
    cycle = 0
    try:
        while True:
            if max_cycles and cycle >= max_cycles:  # bounded in tests
                break
            _run_cycle(client, ledger)
            cycle += 1
            time.sleep(_POLL_INTERVAL)
    finally:
        ledger.close()


def _run_cycle(client, ledger):
    """One poll cycle: TCP probe → drain if reachable."""
    assert client is not None, "client required"
    assert ledger is not None, "ledger required"
    if not client.ping():
        return
    try:
        drain(client, ledger)
    except CcapiNotAuthorized as exc:
        log.error("CCAPI_NOT_AUTHORIZED: %s", exc)
        write_status("error", message="CCAPI_NOT_AUTHORIZED")
    except (CcapiNotReachable, CcapiError) as exc:
        log.warning("CCAPI error: %s", exc)
        write_status("error", message=str(exc)[:200])
    except Exception as exc:
        log.exception("unexpected drain error: %s", exc)
        write_status("error", message="internal error")


def drain(client, ledger):
    """List camera contents once, pull all new files, then reindex.
    Content list is snapshotted before the loop — never re-queried mid-drain."""
    assert client is not None, "client required"
    assert ledger is not None, "ledger required"

    prev = read_status()
    pulled_today = int(prev.get("pulled_today", 0))

    write_status("connected", batch_total=0, batch_done=0,
                 pulled_today=pulled_today)

    contents = client.list_contents()   # snapshot ONCE
    new_items = [
        c for c in contents
        if not ledger.is_pulled(c.content_id)
        and not ledger.is_skipped(c.content_id)
    ]

    if not new_items:
        write_status("idle", pulled_today=pulled_today, message="no new photos")
        return

    total = len(new_items)
    write_status("draining", batch_total=total, batch_done=0,
                 pulled_today=pulled_today)

    done = 0
    for item in new_items:              # bounded: len(new_items) is finite
        if _pull_one(client, ledger, item):
            done += 1
            pulled_today += 1
        write_status("draining", batch_total=total, batch_done=done,
                     pulled_today=pulled_today)

    write_status("done", batch_total=total, batch_done=done,
                 pulled_today=pulled_today,
                 message="Added %d photo%s" % (done, "" if done == 1 else "s"))
    if done:
        _reindex()


def _pull_one(client, ledger, item):
    """Download, size-verify, atomically rename one item. Returns True on success."""
    assert client is not None, "client required"
    assert ledger is not None, "ledger required"
    assert item is not None, "item required"
    try:
        dest_dir, dest_name = _resolve_dest(item)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = _collision_safe_path(dest_dir, dest_name)
        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".cam_import.")
        try:
            os.close(fd)
            client.download(item, tmp_path)
            actual = os.path.getsize(tmp_path)
            if item.size and actual != item.size:
                raise ValueError(
                    "size mismatch: got %d expected %d" % (actual, item.size)
                )
            os.replace(tmp_path, dest_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        ledger.mark_pulled(item.content_id, dest_path, actual)
        log.info("pulled %s → %s", item.name, dest_path)
        return True
    except Exception as exc:
        log.warning("failed to pull %s: %s", item.name, exc)
        count = ledger.increment_fail(item.content_id)
        if count >= MAX_FAILS:
            ledger.skip(item.content_id)
            log.error("skipping %s after %d failures (clear with ledger CLI)",
                      item.name, count)
        return False


def _resolve_dest(item):
    """Return (dest_dir, filename) from item's capture_time. Falls back to 'unknown/'."""
    assert item is not None, "item required"
    try:
        # capture_time formats seen: "2026:09:16 14:22:01" or "2026-09-16T14:22:01"
        ct = item.capture_time.replace(":", "-", 2).replace(" ", "T")
        date_part = ct[:10]    # "YYYY-MM-DD"
        year, month = date_part[:4], date_part[5:7]
        assert year.isdigit() and month.isdigit(), "non-numeric year/month"
        return os.path.join(_DEST_ROOT, year, month), item.name
    except Exception:
        return os.path.join(_DEST_ROOT, "unknown"), item.name


def _collision_safe_path(dest_dir, name):
    """Return a path under dest_dir that does not collide with an existing file."""
    assert dest_dir and name, "dest_dir and name required"
    base, ext = os.path.splitext(name)
    candidate = os.path.join(dest_dir, name)
    n = 0
    while os.path.exists(candidate):   # bounded: n < file count in dir
        n += 1
        candidate = os.path.join(dest_dir, "%s_%04d%s" % (base, n, ext))
    return candidate


def _reindex():
    """Run photo_scanner.py --incremental once after a drain. Logs; never raises."""
    if not os.path.isfile(_SCANNER_SCRIPT):
        log.warning("photo_scanner.py not found at %s; skipping reindex",
                    _SCANNER_SCRIPT)
        return
    try:
        result = subprocess.run(
            [sys.executable, _SCANNER_SCRIPT, "--incremental"],
            timeout=_SCANNER_TIMEOUT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("scanner exited %d: %s",
                        result.returncode, result.stderr[:200])
        else:
            log.info("reindex complete")
    except subprocess.TimeoutExpired:
        log.warning("scanner timed out after %ds", _SCANNER_TIMEOUT)
    except Exception as exc:
        log.warning("scanner failed: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    poll_loop()
```

- [ ] **Step 4: Run all camera tests together**

```bash
python3 -m pytest tests/test_camera_ccapi_client.py tests/test_camera_ledger.py \
    tests/test_camera_importer.py -q
```

Expected: all tests pass (≥30 passed, 0 failed).

- [ ] **Step 5: Commit**

```bash
git add camera/importer.py tests/test_camera_importer.py
git commit -m "feat: add camera importer poll loop and drain with per-file isolation"
```

---

## Task 4: systemd service, interface watchdog, and NM profile

**Files:**
- Create: `system/ares-camera-import.service`
- Create: `system/ares-camera-check-iface` (shell script)
- Create: `system/camera-import.env` (environment template)

No Python tests for this task. Validation is manual (listed in steps).

- [ ] **Step 1: Create `system/camera-import.env`**

```ini
# /etc/ares/camera-import.env
# Copy this file to /etc/ares/camera-import.env and fill in values.
# Confirm CAMERA_IP by checking the camera's SoftAP gateway IP after first
# association (run: ip route show dev <wlan-iface> after nmcli connection up).

CAMERA_IP=
CAMERA_CCAPI_PORT=8080
CAMERA_POLL_INTERVAL=10
CAMERA_DEST_ROOT=/mnt/nvme/PROMETHEUS/PHOTOS
CAMERA_PROBE_TIMEOUT=2
# Leave blank for MAC-based auto-detect. Set to e.g. wlan1 to pin the iface.
CAMERA_WLAN_IFACE=
```

- [ ] **Step 2: Create `system/ares-camera-check-iface`**

```bash
#!/bin/bash
# ExecStartPre for ares-camera-import.service.
# Fails the service start if the RTL8852BE interface (MAC 50:03:CF:57:81:98)
# is not present. Silent no-op operation (poll loop runs, nothing logged wrong)
# is worse than a loud immediate failure.
set -euo pipefail

IFACE="${CAMERA_WLAN_IFACE:-}"

if [ -z "$IFACE" ]; then
    # Auto-detect by MAC address (lower-case, colon-separated)
    IFACE=$(ip -o link show | awk -F'[ /]' '
        /50:03:cf:57:81:98/ || /50:03:CF:57:81:98/ { print $3 }
    ')
fi

if [ -z "$IFACE" ]; then
    echo "ERROR: RTL8852BE WiFi interface not found." >&2
    echo "       Did the card bind after reboot? Check:" >&2
    echo "         lspci | grep -i realtek" >&2
    echo "         ip link show" >&2
    exit 1
fi

if ! ip link show "$IFACE" > /dev/null 2>&1; then
    echo "ERROR: Interface $IFACE listed by MAC but ip link show failed." >&2
    exit 1
fi

echo "ares-camera-check-iface: interface $IFACE is present."
```

- [ ] **Step 3: Create `system/ares-camera-import.service`**

```ini
[Unit]
Description=ARES camera import — Canon G7X III CCAPI
After=network-online.target NetworkManager.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ares/camera-import.env
ExecStartPre=/usr/local/sbin/ares-camera-check-iface
ExecStart=/usr/bin/python3 -m camera.importer
WorkingDirectory=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ares-camera-import

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Install files on host**

```bash
# Copy env template (fill in CAMERA_IP after hardware gate)
mkdir -p /etc/ares
cp system/camera-import.env /etc/ares/camera-import.env
chmod 640 /etc/ares/camera-import.env

# Install interface watchdog
cp system/ares-camera-check-iface /usr/local/sbin/ares-camera-check-iface
chmod 755 /usr/local/sbin/ares-camera-check-iface

# Install service unit
cp system/ares-camera-import.service /etc/systemd/system/
systemctl daemon-reload
```

Do NOT `systemctl enable` or `systemctl start` yet. The card is not bound and `CAMERA_IP` is not set.

- [ ] **Step 5: Create NM profile (do this AFTER reboot with camera SoftAP live)**

Fill in `<SSID>` and `<password>` from the camera LCD screen. `<wlan-iface>` is
whatever `ip link show` shows for the RTL8852BE after reboot (e.g. `wlan1`).

```bash
nmcli connection add \
  type wifi \
  ssid "<SSID>" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "<password>" \
  802-11-wireless.bssid 50:03:CF:57:81:98 \
  connection.interface-name <wlan-iface> \
  wifi.powersave 2 \
  ipv4.route-metric 50 \
  ipv6.method ignore \
  connection.autoconnect yes

nmcli connection up "<SSID>"
```

Verify association:
```bash
nmcli dev status          # wlan-iface should show "connected"
ip addr show <wlan-iface> # should have 192.168.x.x lease from camera
```

- [ ] **Step 6: Commit service files**

```bash
git add system/ares-camera-import.service \
        system/ares-camera-check-iface \
        system/camera-import.env
git commit -m "feat: add systemd service unit and interface watchdog for camera import"
```

---

## Task 5: Hardware verification gate

This task produces no code. It is the physical validation that must pass before `CAMERA_IP` is set and the service is enabled. Complete steps in order; stop and investigate on any failure.

- [ ] **Step 1: Reboot ARES with safe-reboot**

```bash
/usr/local/sbin/safe-reboot
```

Expected: host reboots, VM200 auto-starts (onboot: 1) without the WiFi card.

- [ ] **Step 2: Confirm RTL8852BE is bound to host**

```bash
lspci | grep -i realtek   # should show 06:00.0 Network controller: RTL8852BE
ip link show              # should show a wlan* interface with MAC 50:03:cf:57:81:98
```

If the interface is absent: `lsmod | grep rtw89` — if missing, driver did not load.
Fix: `modprobe rtw89_8852be` and investigate `/etc/modprobe.d/` for stray blacklist.

- [ ] **Step 3: Confirm VM200 started without the WiFi card**

```bash
qm status 200             # should be "running"
# In the VM: Device Manager should NOT show an 802.11ax adapter
```

- [ ] **Step 4: Enable camera SoftAP**

On the camera: press the WiFi button → navigate to the computer/smartphone
connection option (NOT "wireless remote" — that is the BT shutter remote).
The camera LCD will display an SSID and password. Note them.

- [ ] **Step 5: Create NM profile and associate**

Run the `nmcli connection add` command from Task 4 Step 5 with the SSID and
password from the camera LCD. Then:

```bash
nmcli connection up "<SSID>"
ip route show dev <wlan-iface>   # note the camera's gateway IP
```

The gateway IP is `CAMERA_IP`. Write it to `/etc/ares/camera-import.env`.

- [ ] **Step 6: Verify CCAPI reachability (gate 1 + gate 2)**

```bash
# TCP probe
nc -z -w 2 <CAMERA_IP> 8080 && echo "TCP OK"

# CCAPI version endpoint
curl -v http://<CAMERA_IP>:8080/ccapi/
```

Expected: JSON response with `"ccapi": [{"version": "ver100", ...}]`.

If you get a 401/403: a pairing dialog appeared on the camera LCD — complete
it and re-run. If pairing is required on every power cycle, document it here.

If you get a 404 or connection refused: CCAPI is not exposed on this camera
firmware at port 8080. Try port 80. If neither works, see the spec fallback
options (USB tether, Mac bridge).

- [ ] **Step 7: Verify content listing**

```bash
# List DCIM root — adjust ver100 to match what /ccapi/ returned
curl http://<CAMERA_IP>:8080/ccapi/ver100/contents/sd/0
```

Expected: JSON with `"path": [...]` listing DCIM folders.

- [ ] **Step 8: Measure latency budget (gate 3)**

Take 5–10 test photos. Time a drain:

```bash
time python3 -c "
import os; os.environ['CAMERA_IP']='<CAMERA_IP>'
from camera.ccapi_client import CcapiClient
c = CcapiClient('<CAMERA_IP>')
refs = c.list_contents()
print(len(refs), 'files found')
"
```

Note the per-file average. This informs the toast's progress rate accuracy.

- [ ] **Step 9: Enable and start the service**

Only after all gates pass:

```bash
systemctl enable ares-camera-import.service
systemctl start ares-camera-import.service
systemctl status ares-camera-import.service   # should be "active (running)"
journalctl -u ares-camera-import -f           # watch the poll loop
```

- [ ] **Step 10: End-to-end smoke test**

1. Take a new photo with the camera.
2. Enable the camera WiFi (press the button → navigate to the computer connection).
3. Watch the dashboard at `https://ares.tail3045df.ts.net/` — Shift+P shows the
   simulated toast; the real toast should appear when the importer picks up the
   new photo.
4. Confirm the photo lands in `PHOTOS/<YYYY>/<MM>/` and appears in the gallery.

```bash
# Confirm pull landed in the gallery
sqlite3 /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/photo_index.db \
  "SELECT path, added_at FROM photos ORDER BY added_at DESC LIMIT 3;"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| `CcapiNotAuthorized` for 401/403 | Task 1 |
| Content list snapshot at drain-start | Task 3 (drain) |
| Dedup key = CCAPI content ID | Task 2 |
| 3-strike skip with warning log | Tasks 2 + 3 |
| Partial-file protection (temp → verify → replace → ledger) | Task 3 (_pull_one) |
| Per-file isolation (one failure doesn't stall batch) | Task 3 (drain loop) |
| Collision-safe naming | Task 3 (_collision_safe_path) |
| Capture-date foldering with 'unknown/' fallback | Task 3 (_resolve_dest) |
| `_reindex()` once after drain, not per-file | Task 3 (drain, called after loop) |
| `_reindex()` non-blocking on failure | Task 3 (_reindex logs, never raises) |
| `ExecStartPre` interface assertion | Task 4 |
| NM profile `wifi.powersave=2` + `ipv4.route-metric=50` | Task 4 |
| Hardware verification gates | Task 5 |
| Status file IPC (existing `camera/status.py`) | Already built |
| Flask routes (existing `app.py`) | Already built |
| Toast UI (existing `templates/home.html`) | Already built |

**No placeholders found.**

**Type consistency:** `ContentRef` defined in Task 1; consumed by name in Tasks 3 tests and importer. `Ledger` and `MAX_FAILS` defined in Task 2; imported explicitly in Task 3. `drain(client, ledger)` signature consistent between Task 3 implementation and tests.
