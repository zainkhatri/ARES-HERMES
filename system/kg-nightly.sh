#!/bin/bash
# kg-nightly.sh — keep the homelab knowledge graph current.
# Reindexes the ARES content spine, syncs EROS over the tailnet, and restarts the
# dashboard so it serves the fresh graph. Run nightly by kg-nightly.timer.
# Understanding-generation auto-pauses under .gpu-on-loan (see atlas/understanding.py).
set -u
export HOME=/root
MNEMO="/mnt/nvme/PROMETHEUS/PROJECTS/more projects/atlas"
CENTRAL="$MNEMO/data/homelab_kg.db"
LOG=/var/log/kg-nightly.log
exec >>"$LOG" 2>&1

echo "=== kg-nightly start $(date -Is) ==="

# 1) ARES content spine. KG_MAX_DEPTH=4 reproduces the ~610-node graph (absolute depths 3-7);
#    WITHOUT this cap the walk goes to depth 11 = 15k nodes + a 6h Ollama run. Do not remove.
#    Only changed folders regenerate understandings; vault guarded; auto-pause under .gpu-on-loan.
env KG_DB="$CENTRAL" KG_MAX_DEPTH=4 OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli reindex /mnt/nvme/PROMETHEUS --box ARES \
  && echo "kg-nightly: ARES reindex ok" || echo "kg-nightly: ARES reindex FAILED"

# 1b) index Claude Code chats (ARES) + the ChatGPT archive — STRUCTURE only (searchable now);
#     summaries fill in progressively via 1d. Budget 0 = no Ollama in the index step.
env KG_DB="$CENTRAL" PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli index-chats --box ARES --summary-budget 0 \
  && echo "kg-nightly: ARES chats indexed" || echo "kg-nightly: ARES chat index FAILED"
env KG_DB="$CENTRAL" PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli index-gpt --box ARES --summary-budget 0 \
  && echo "kg-nightly: GPT archive indexed" || echo "kg-nightly: GPT index FAILED"

# 1c2) claude.ai web export, IF you've dropped one in (Settings -> Export data -> conversations.json)
CLAUDE_WEB=/mnt/nvme/PROMETHEUS/PERSONAL/CLAUDE
[ -d "$CLAUDE_WEB" ] && env KG_DB="$CENTRAL" PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli index-claude-web --box ARES --dir "$CLAUDE_WEB" --summary-budget 0 \
  && echo "kg-nightly: claude.ai export indexed" || true

# 1c3) index this box's Claude environment (skills + MCP servers) so a new account/box knows what you use + how to re-add
env KG_DB="$CENTRAL" PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli index-env --box ARES \
  && echo "kg-nightly: env (skills+mcps) indexed" || true

# 1d) progressive Ollama summaries for still-raw chat/gpt nodes (GPT + ZEUS backfill), budgeted
#     so a ~9k-conversation backfill spreads across nights instead of blocking. gpu-loan guarded.
env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli summarize-pending --budget 900 \
  && echo "kg-nightly: summaries batch ok" || echo "kg-nightly: summaries FAILED"

# 2) EROS over the tailnet (best-effort; the script self-skips if EROS is unreachable)
/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/kg-sync-eros.sh \
  && echo "kg-nightly: EROS sync ok" || echo "kg-nightly: EROS sync skipped/failed"

# 3) dashboard picks up the fresh graph
pct exec 101 -- systemctl restart ares 2>/dev/null && echo "kg-nightly: ares restarted" || true

# 4) report final size
sqlite3 "$CENTRAL" "SELECT 'kg-nightly: '||box||'='||count(*) FROM nodes GROUP BY box;"
echo "=== kg-nightly done $(date -Is) ==="
