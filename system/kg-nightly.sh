#!/bin/bash
# kg-nightly.sh — keep the homelab knowledge graph current.
# Reindexes the ARES content spine, syncs EROS over the tailnet, and restarts the
# dashboard so it serves the fresh graph. Run nightly by kg-nightly.timer.
# Understanding-generation auto-pauses under .gpu-on-loan (see mnemosyne/understanding.py).
set -u
export HOME=/root
MNEMO=/mnt/nvme/PROMETHEUS/PROJECTS/mnemosyne
CENTRAL="$MNEMO/data/homelab_kg.db"
LOG=/var/log/kg-nightly.log
exec >>"$LOG" 2>&1

echo "=== kg-nightly start $(date -Is) ==="

# 1) ARES content spine. KG_MAX_DEPTH=4 reproduces the ~610-node graph (absolute depths 3-7);
#    WITHOUT this cap the walk goes to depth 11 = 15k nodes + a 6h Ollama run. Do not remove.
#    Only changed folders regenerate understandings; vault guarded; auto-pause under .gpu-on-loan.
env KG_DB="$CENTRAL" KG_MAX_DEPTH=4 OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m mnemosyne.cli reindex /mnt/nvme/PROMETHEUS --box ARES \
  && echo "kg-nightly: ARES reindex ok" || echo "kg-nightly: ARES reindex FAILED"

# 1b) index Claude Code chats into the graph (searchable session context; bounded head-read)
env KG_DB="$CENTRAL" PYTHONPATH="$MNEMO" \
  python3 -m mnemosyne.cli index-chats --box ARES \
  && echo "kg-nightly: chats indexed ok" || echo "kg-nightly: chat index FAILED"

# 2) EROS over the tailnet (best-effort; the script self-skips if EROS is unreachable)
/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/kg-sync-eros.sh \
  && echo "kg-nightly: EROS sync ok" || echo "kg-nightly: EROS sync skipped/failed"

# 3) dashboard picks up the fresh graph
pct exec 101 -- systemctl restart ares 2>/dev/null && echo "kg-nightly: ares restarted" || true

# 4) report final size
sqlite3 "$CENTRAL" "SELECT 'kg-nightly: '||box||'='||count(*) FROM nodes GROUP BY box;"
echo "=== kg-nightly done $(date -Is) ==="
