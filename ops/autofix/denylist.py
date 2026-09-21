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


# --- command gate --------------------------------------------------------
# A host/config fix that cannot be expressed as an in-repo diff arrives as a
# list of shell command strings that WILL run verbatim on the box once council
# approves (see finalize.py / apply.run_commands). check_commands is the
# mechanical hard gate for that path -- the exact analogue of check_diff_paths
# for diffs. It runs BEFORE council and again immediately before execution.

# Core services whose stop/disable/mask would leave the dashboard, web shell,
# or the fixer itself down. Mirrors apply.RESTART_EXCLUDE (kept in sync by
# hand -- two modules, no shared import, one page each).
CORE_SERVICE_UNITS = {
    "ares", "caddy", "ttyd", "pty_ws", "ares-shell-ctl",
    "ares-autofix-watcher", "ares-autofix-apply", "ares-autofix-audit",
}

# Units the fixer must never RESTART either, because restarting them mid-apply
# interrupts the very run that is applying the fix. caddy/ttyd/ares recover in
# ~1s and a deliberate approved fix is allowed to restart them, so they are NOT
# here -- only the autofix control-plane units are.
RESTART_PROTECTED_UNITS = {
    "ares-autofix-watcher", "ares-autofix-apply", "ares-autofix-audit",
}

# Destructive verbs -- irreversible data loss, host power state, or VM/CT
# teardown. Any match hard-blocks the whole command list.
COMMAND_DENYLIST_PATTERNS = [
    r"\brm\s+-\S*[rR]\S*[fF]",          # rm -rf / -Rf / -rfv ...
    r"\brm\s+-\S*[fF]\S*[rR]",          # rm -fr / -fR ...
    r"\brm\s+--recursive\b",
    r"\bdd\b",
    r"\bmkfs\S*\b", r"\bwipefs\b", r"\bsgdisk\b", r"\bfdisk\b",
    r"\bparted\b", r"\bmkswap\b", r"\bswapoff\b",
    r"\breboot\b", r"\bpoweroff\b", r"\bhalt\b", r"\bshutdown\b",
    r"\bsystemctl\s+(reboot|poweroff|halt|kexec|emergency|rescue)\b",
    r"\bqm\s+(stop|reset|destroy|shutdown|rollback)\b",
    r"\bpct\s+(stop|destroy|shutdown)\b",
    r"\bpkill\b", r"\bkillall\b",
    r":\s*\(\s*\)\s*\{",                # fork-bomb signature :(){
]


def _hits_protected_service(cmd):
    """True if the command stops/disables/masks/kills any core service, or
    restarts a control-plane unit -- either would take the platform or the
    fixer offline."""
    m = re.search(r"\bsystemctl\s+(?:--\S+\s+)*"
                  r"(restart|stop|disable|mask|kill)\s+([^\s;&|]+)", cmd)
    if m is None:
        return False
    action = m.group(1)
    unit = m.group(2).lstrip("/")
    if unit.endswith(".service"):
        unit = unit[: -len(".service")]
    if action in ("stop", "disable", "mask", "kill"):
        return unit in CORE_SERVICE_UNITS
    return unit in RESTART_PROTECTED_UNITS


def check_commands(commands):
    """Mechanical (non-LLM) gate on a list of shell command strings. Blocks
    destructive verbs, core-service takedowns, and any denylisted path -- the
    command analogue of check_diff_paths. Returns
    (is_clean: bool, violating_commands: list[str])."""
    assert isinstance(commands, list), "commands must be a list"
    violations = []
    for cmd in commands:
        assert isinstance(cmd, str), "each command must be a string"
        # path denylist is anchored with (^|/), so test each shell token as its
        # own path rather than the whole command line (a path mid-command sits
        # after a space, which the anchor would otherwise miss).
        tokens = re.split(r"[\s;|&><()\"']+", cmd)
        if any(re.search(p, cmd) for p in COMMAND_DENYLIST_PATTERNS):
            violations.append(cmd)
        elif any(re.search(p, tok) for tok in tokens for p in DENYLIST_PATTERNS):
            violations.append(cmd)
        elif _hits_protected_service(cmd):
            violations.append(cmd)
    return (len(violations) == 0, violations)
