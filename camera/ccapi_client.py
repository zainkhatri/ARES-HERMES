"""Thin Canon CCAPI HTTP wrapper.

No orchestration, no filesystem writes. Every public method raises on error;
callers handle exceptions. 401/403 -> CcapiNotAuthorized (named, not generic).
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

    _CHUNK = 65536  # download chunk size -- bounded buffer (Power-of-Ten rule 3)

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
        """GET /ccapi/ -> dict. Pins _api_prefix to the best available version."""
        r = self._get(self._base + "/ccapi/")
        d = r.json()
        versions = [e.get("version", "") for e in d.get("ccapi", [])]
        if not versions:
            raise CcapiError("no versions in /ccapi/ response")
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
            except CcapiNotAuthorized:
                raise
            except CcapiError:
                pass  # skip unreadable metadata; log at caller
        return refs

    def file_metadata(self, item_url):
        """GET <item_url>/info -> ContentRef."""
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
        assert written > 0 or ref.size == 0, "download produced 0 bytes for non-empty file"
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
