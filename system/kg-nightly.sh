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

# 1b) Claude Code sessions from EVERY box: archive + whole-transcript index + redacted
#     OpenRouter summaries. Same script as the hourly kg-sessions.timer (flock-guarded;
#     its own log is /var/log/kg-sessions.log). Then the ChatGPT archive, structure only.
/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/kg-sync-sessions.sh \
  && echo "kg-nightly: sessions synced" || echo "kg-nightly: session sync FAILED"
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

# 2) EROS over the tailnet (best-effort; the script self-skips if EROS is unreachable)
/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/system/kg-sync-eros.sh \
  && echo "kg-nightly: EROS sync ok" || echo "kg-nightly: EROS sync skipped/failed"

# 3) dashboard picks up the fresh graph — done BEFORE the slow summary backfill below so a
#    graph refresh + restart always lands inside the timeout even if summaries run long.
pct exec 101 -- systemctl restart ares 2>/dev/null && echo "kg-nightly: ares restarted" || true

# 4) report size so far
sqlite3 "$CENTRAL" "SELECT 'kg-nightly: '||box||'='||count(*) FROM nodes GROUP BY box;"

# 1d) summaries for still-raw GPT / claude.ai nodes and orphaned Claude Code chats (source file
#     gone). Redacted OpenRouter when a key exists (~1s/call), else Ollama (gpu-loan guarded).
env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli summarize-pending --budget 1500 \
  && echo "kg-nightly: summaries batch ok" || echo "kg-nightly: summaries FAILED"

# 1e) search vectors for summarized chats that missed one (Ollama down / GPU on loan when summarized)
env KG_DB="$CENTRAL" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$MNEMO" \
  python3 -m atlas.cli embed-pending --budget 5000 \
  && echo "kg-nightly: embeddings backfilled" || echo "kg-nightly: embed-pending FAILED"

echo "=== kg-nightly done $(date -Is) ==="
