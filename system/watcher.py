#!/usr/bin/env python3
"""Watch ARES source files and restart the service when .py files change.

Template/HTML changes are handled by Flask's TEMPLATES_AUTO_RELOAD — no restart needed.
Python file changes require a full service restart, which this script handles.
"""

import os
import time
import subprocess

# Derive the repo dir from this file — the old hardcoded '/srv/mergerfs/PROMETHEUS/ARES' is the
# ZEUS/HERMES path and does NOT exist on ARES (/mnt/...), so the watcher monitored 0 files and
# auto-restart silently never fired (every edit needed a manual restart). Env override kept.
WATCH_DIR = os.getenv("ARES_WATCH_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WATCH_EXTS = {'.py'}
SERVICE = 'ares'
POLL_INTERVAL = 2  # seconds
RESTART_COOLDOWN = 6  # seconds after restart before re-polling


def get_mtimes():
    mtimes = {}
    skip_dirs = {'__pycache__', '.venv', '.sessions', 'static', 'node_modules', 'site-packages'}
    for root, dirs, files in os.walk(WATCH_DIR):
        # Skip hidden dirs, known non-source dirs, AND any virtualenv (`.venv`, `clip-gpu-venv`, …) —
        # a venv holds thousands of .py and would turn the 2s poll into a stat storm.
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in skip_dirs and 'venv' not in d.lower()]
        for fname in files:
            if any(fname.endswith(ext) for ext in WATCH_EXTS):
                path = os.path.join(root, fname)
                try:
                    mtimes[path] = os.path.getmtime(path)
                except OSError:
                    pass
    return mtimes


def restart_service():
    print(f'[watcher] Restarting {SERVICE}...', flush=True)
    subprocess.run(['systemctl', 'restart', SERVICE], check=False)
    time.sleep(RESTART_COOLDOWN)
    print(f'[watcher] {SERVICE} restarted.', flush=True)


if __name__ == '__main__':
    mtimes = get_mtimes()
    print(f'[watcher] Monitoring {len(mtimes)} Python files in {WATCH_DIR}', flush=True)

    while True:
        time.sleep(POLL_INTERVAL)
        new_mtimes = get_mtimes()

        changed = [
            p for p in new_mtimes
            if new_mtimes[p] != mtimes.get(p)
        ]
        added = [p for p in new_mtimes if p not in mtimes]
        removed = [p for p in mtimes if p not in new_mtimes]

        if changed or added or removed:
            for p in changed:
                print(f'[watcher] modified: {os.path.relpath(p, WATCH_DIR)}', flush=True)
            for p in added:
                print(f'[watcher] added:    {os.path.relpath(p, WATCH_DIR)}', flush=True)
            for p in removed:
                print(f'[watcher] removed:  {os.path.relpath(p, WATCH_DIR)}', flush=True)
            restart_service()
            mtimes = get_mtimes()
        else:
            mtimes = new_mtimes
