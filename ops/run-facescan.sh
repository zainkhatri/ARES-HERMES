#!/bin/bash
# Nightly people/face + screenshot sweep — used by the nightly cron and manual catch-up runs.
# Phases run SEQUENTIALLY (faces/expand can't combine: --expand is an early-return
# branch in ai_indexer.py, so `--faces --expand` would skip detection entirely):
#   1) --faces                : detect + cluster faces on NEW photos (incremental)
#   2) --expand               : assign new unmatched faces to the nearest already-named person
#   3) --classify-screenshots : sweep documents / text-message shots / "waffle" clutter
#                               out of the main gallery and into Screenshots
#   4) restart ares           : app loads face_clusters + screenshot_hashes at startup,
#                               so it must restart to pick up the new sweep results
# flock -n makes concurrent runs a no-op (multi-session scan hazard).
cd /mnt/data/PROJECTS/ARES-DASHBOARD || exit 1
export PYTHONUNBUFFERED=1   # flush progress to the log live (no stdout buffering)
exec /bin/flock -n /tmp/ares-facescan.lock bash -c '
  echo "=== faces $(date -u +%FT%TZ) ==="
  .venv/bin/python3 photos/ai_indexer.py --faces
  echo "=== expand $(date -u +%FT%TZ) ==="
  .venv/bin/python3 photos/ai_indexer.py --expand
  echo "=== screenshots $(date -u +%FT%TZ) ==="
  .venv/bin/python3 photos/ai_indexer.py --classify-screenshots
  echo "=== restart ares $(date -u +%FT%TZ) ==="
  systemctl restart ares
  echo "=== done $(date -u +%FT%TZ) ==="
'
