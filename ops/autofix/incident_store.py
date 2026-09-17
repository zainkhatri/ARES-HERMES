"""Single source of truth for autofix incidents. flock-guarded, single
writer per field: only set_status() may change `status`; every other
writer touches only its own diagnosis.* sub-fields."""
import fcntl
import json
import os
import time
import uuid

VALID_STATUSES = {
    "new", "triaged_skip", "escalated", "diagnosed", "diagnosis_timeout",
    "council_approved", "council_held", "stale_diff_needs_human",
    "resolved", "rejected", "revert_failed_needs_human",
    # audit-only: a finding with no applicable diff (e.g. a remote-host
    # config recommendation on EROS/ZEUS) -- council-reviewed but nothing to
    # Approve/apply, it's read-only guidance for a human to act on manually.
    "recommendation_ready",
}


class CorruptStoreError(Exception):
    pass


class IncidentStore:
    def __init__(self, path):
        self.path = path

    def _read_locked(self, f):
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            raw = f.read()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
        if not raw.strip():
            return {"incidents": []}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise CorruptStoreError(f"{self.path} is corrupt: {e}") from e

    def load(self):
        if not os.path.exists(self.path):
            return {"incidents": []}
        with open(self.path, "r") as f:
            return self._read_locked(f)

    def _write_locked(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(data, f, indent=2)
            fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp, self.path)

    def new_incident(self, signature, source, detail, title=None):
        data = self.load()
        iid = uuid.uuid4().hex[:12]
        data["incidents"].append({
            "id": iid,
            "title": title or signature,   # human-readable; falls back to the raw hash if none given
            "signature": signature,
            "source": source,
            "detail": detail,
            "status": "new",
            "diagnosis": {},
            "created_ts": int(time.time()),
            "updated_ts": int(time.time()),
        })
        self._write_locked(data)
        return iid

    def write_diagnosis(self, incident_id, **fields):
        data = self.load()
        for inc in data["incidents"]:
            if inc["id"] == incident_id:
                inc["diagnosis"].update(fields)
                inc["updated_ts"] = int(time.time())
                self._write_locked(data)
                return
        raise KeyError(f"no incident {incident_id}")

    def set_status(self, incident_id, status):
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        data = self.load()
        for inc in data["incidents"]:
            if inc["id"] == incident_id:
                inc["status"] = status
                inc["updated_ts"] = int(time.time())
                self._write_locked(data)
                return
        raise KeyError(f"no incident {incident_id}")

    def find_by_signature(self, signature):
        data = self.load()
        matches = [i for i in data["incidents"] if i["signature"] == signature]
        return matches[-1] if matches else None
