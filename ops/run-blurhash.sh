#!/bin/bash
# Nightly BlurHash placeholder refresh. Incremental — only encodes photos added since the last
# run (cheap re-run), then rewrites blurhashes.json which /api/photos/blurhashes serves (the
# endpoint picks up the new file by mtime, no restart needed). Runs after facescan+reindex.
set -a; . /mnt/data/PROJECTS/ARES-DASHBOARD/.env 2>/dev/null; set +a
cd /mnt/data/PROJECTS/ARES-DASHBOARD || exit 1
echo "[$(date)] blurhash gen start"
.venv/bin/python gen_blurhashes.py
echo "[$(date)] blurhash gen done"
