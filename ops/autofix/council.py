"""Shared headless-Claude council-style review helper. Used both as the
pre-escalation "is this actually worth a full diagnosis" gate (watcher.py)
and the mandatory post-diagnosis safety gate (finalize.py). Not the full
5-advisor /llm-council ceremony -- that's for high-stakes one-off human
decisions. This is the cheap, mandatory, per-incident automated version of
the same principle: an independent pass before anything expensive or risky
happens, fail-closed on any error."""
import json
import subprocess


def ask(prompt, max_turns=5, timeout=300):
    """Runs prompt through headless claude -p, expects a JSON object with
    at least {"approve": bool, "verdict": str} on the LAST line of stdout.
    Fails closed (approve=False) on any error -- an uncertain council call
    should never silently green-light something."""
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--max-turns", str(max_turns)],
            capture_output=True, text=True, timeout=timeout,
        )
        parsed = json.loads(r.stdout.strip().splitlines()[-1])
        return bool(parsed["approve"]), str(parsed["verdict"])
    except Exception as e:
        return False, f"council invocation failed, fail-closed: {e}"
