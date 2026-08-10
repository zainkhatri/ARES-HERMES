#!/bin/bash
# People / face scan — used by the nightly cron and manual catch-up runs.
# Two phases, run SEQUENTIALLY (they can't be combined: --expand is an early-return
# branch in ai_indexer.py, so `--faces --expand` would skip detection entirely):
#   1) --faces  : detect + cluster faces on NEW photos (incremental)
#   2) --expand : assign the new unmatched faces to the nearest already-named person
# flock -n makes concurrent runs a no-op (multi-session scan hazard).
cd /mnt/data/PROJECTS/ARES-DASHBOARD || exit 1
export PYTHONUNBUFFERED=1   # flush progress to the log live (no stdout buffering)
exec /bin/flock -n /tmp/ares-facescan.lock bash -c '
  echo "=== faces $(date -u +%FT%TZ) ==="
  .venv/bin/python3 photos/ai_indexer.py --faces
  echo "=== expand $(date -u +%FT%TZ) ==="
  .venv/bin/python3 photos/ai_indexer.py --expand
  echo "=== done $(date -u +%FT%TZ) ==="
'
