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
