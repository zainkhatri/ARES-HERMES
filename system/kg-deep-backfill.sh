#!/bin/bash
# kg-deep-backfill.sh — clears the historical depth 8-11 understanding backlog left by
# the original uncapped Phase-1 walk (15,457 nodes / 6.5h, before the retry-budget and
# KG_MAX_DEPTH=4 nightly-cap fixes landed, 2026-09-19). kg-nightly.sh's walk is capped
# at KG_MAX_DEPTH=4 (absolute depth ~7) on purpose, so it can never reach depth 8+ no
# matter how many nights pass — this is a separate, deeper pass (walker.py's coded
# default depth) with its own --retry-budget so a single run can't blow past its own
# timeout the way the original uncapped run did.
# Check remaining backlog: sqlite3 <db> "select count(*) from nodes where kind='folder'
# and (understanding is null or understanding='')" — should trend to 0 over ~16 nights.
#
# --deadline 3000: since 2026-09-23, EROS's llama-server (14B Qwen, 4.6 of 8 GB VRAM)
# pushes Ollama's llama3.2:3b mostly onto CPU, ~3x slower per folder, so 800 retries
# no longer fit the 3600s TimeoutStartSec and every run was killed. The deadline stops
# generating after 50 min (walk + final upserts fit in the last 10) and defers the rest
# to the next night; the log line reports how many were deferred.
set -u
export HOME=/root
MNEMO="/mnt/nvme/PROMETHEUS/PROJECTS/more projects/atlas"
CENTRAL="$MNEMO/data/homelab_kg.db"
LOG=/var/log/kg-deep-backfill.log
exec >>"$LOG" 2>&1

echo "=== kg-deep-backfill start $(date -Is) ==="
env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli reindex /mnt/nvme/PROMETHEUS --box ARES --retry-budget 800 --deadline 3000 \
  && echo "kg-deep-backfill: ok" || echo "kg-deep-backfill: FAILED"
echo "=== kg-deep-backfill done $(date -Is) ==="
