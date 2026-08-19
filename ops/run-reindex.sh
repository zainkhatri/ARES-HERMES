#!/bin/bash
# Nightly file-locator index rebuild (Phase 1 assistant). Throttled walk inside.
set -a; . /mnt/data/PROJECTS/ARES-DASHBOARD/.env 2>/dev/null; set +a
cd /mnt/data/PROJECTS/ARES-DASHBOARD || exit 1
echo "[$(date)] reindex start"
.venv/bin/python -c "from system import files_index; print('indexed', files_index.build_index()); print('embedded', files_index.embed_docs())"
echo "[$(date)] reindex done"
