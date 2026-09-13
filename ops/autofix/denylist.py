"""Mechanical (non-LLM) check of which files a diff touches. This is a hard
gate that runs BEFORE /llm-council -- council is advisory on top of this,
never a substitute for it."""
import re

DENYLIST_PATTERNS = [
    r"(^|/)vault(/|$)",
    r"(^|/)etc/pve(/|$)",
    r"(^|/)FAI(/|$)",
    r"(^|/)FCSF(/|$)",
    r"(^|/)AUTOMATION-IBT(/|$)",
    r"^\.git/",
    r"(^|/)ops/autofix(/|$)",
]


def _touched_paths(diff_text):
    paths = set()
    for line in diff_text.splitlines():
        m = re.match(r"^(?:---|\+\+\+)\s+[ab]/(.+)$", line)
        if m:
            paths.add(m.group(1).strip())
    return sorted(paths)


def check_diff_paths(diff_text):
    """Returns (is_clean: bool, violating_paths: list[str])."""
    violations = []
    for path in _touched_paths(diff_text):
        for pattern in DENYLIST_PATTERNS:
            if re.search(pattern, path):
                violations.append(path)
                break
    return (len(violations) == 0, violations)
