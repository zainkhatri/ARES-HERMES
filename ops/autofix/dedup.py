"""Dedup key for recurring incidents. Strips per-run dynamic values
(timestamps, PIDs, memory addresses, line numbers) from an error line so the
same underlying bug hashes to the same signature every time it recurs."""
import hashlib
import re

_TIMESTAMP_RE = re.compile(r"\b[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b")
_PID_RE = re.compile(r"\bPID:?\s*\d+\b", re.IGNORECASE)
_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_LINE_NO_RE = re.compile(r"\bline\s+\d+\b")
_BARE_NUMBER_RE = re.compile(r"\b\d{3,}\b")  # long standalone numbers (pids without a label, etc.)


def _strip_dynamic(text):
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _PID_RE.sub("pid:<n>", text)
    text = _ADDR_RE.sub("<addr>", text)
    text = _LINE_NO_RE.sub("line <n>", text)
    text = _BARE_NUMBER_RE.sub("<n>", text)
    return text.strip()


def normalize_signature(unit_name, first_error_line):
    normalized = _strip_dynamic(first_error_line)
    key = f"{unit_name}|{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
