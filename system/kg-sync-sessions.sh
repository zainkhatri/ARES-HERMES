#!/bin/bash
# kg-sync-sessions.sh — save + index EVERY Claude Code session on every box.
# 1) rsync each box's ~/.claude/projects into the permanent archive (never --delete),
# 2) re-assert cleanupPeriodDays=36500 so Claude Code never deletes transcripts,
# 3) index the archive into homelab-kg; idle sessions get a redacted OpenRouter summary.
# Offline boxes are skipped (ZEUS is reached WITHOUT waking it); the next run catches up.
# Run hourly by kg-sessions.timer and from kg-nightly.sh. Spec:
# atlas/docs/superpowers/specs/2026-09-24-session-memory-design.md
set -u -o pipefail
export HOME=/root
ATLAS="/mnt/nvme/PROMETHEUS/PROJECTS/more projects/atlas"
DB="$ATLAS/data/homelab_kg.db"
ARCH=/mnt/nvme/PROMETHEUS/PERSONAL/CLAUDE-CODE-SESSIONS
LOG=/var/log/kg-sessions.log
BUDGET="${KG_SESSION_BUDGET:-3000}"     # max new summaries per source per run
KEEP=36500

exec 9>/run/kg-sessions.lock
flock -n 9 || { echo "kg-sync-sessions: already running" >>"$LOG"; exit 0; }
exec >>"$LOG" 2>&1
echo "=== kg-sync-sessions start $(date -Is) ==="
mkdir -p "$ARCH" && chmod 700 "$ARCH"

SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o StrictHostKeyChecking=accept-new"
# ZEUS by tailscale IP: the `zeus` alias fires Wake-on-LAN, and an hourly job must not
# keep the NAS awake. If it is asleep it is skipped; its transcripts are kept there anyway.
ZEUS=root@100.100.29.36

# keep-forever setting for one settings.json path (runs on the target box)
keep_py() {
  printf '%s' "import json,os;p=os.path.expanduser('$1');os.makedirs(os.path.dirname(p),exist_ok=True);d=json.load(open(p)) if os.path.exists(p) else {};d.get('cleanupPeriodDays')==$KEEP or (d.update(cleanupPeriodDays=$KEEP) or json.dump(d,open(p,'w'),indent=2)) ;print('keep ok',p)"
}

# pull SRC SSH_CMD DEST REMOTE_PROJECTS REMOTE_SETTINGS
pull() {
  local src="$1" ssh="$2" dest="$3" rproj="$4" rset="$5"
  if ! timeout 25 $ssh "$dest" true 2>/dev/null; then
    echo "kg-sync-sessions: $src unreachable, skipped"; return 1
  fi
  mkdir -p "$ARCH/$src"
  timeout 1800 rsync -a -e "$ssh" "$dest:$rproj/" "$ARCH/$src/" \
    && echo "kg-sync-sessions: $src archived" || echo "kg-sync-sessions: $src rsync FAILED"
  timeout 30 $ssh "$dest" "python3 -c \"$(keep_py "$rset")\"" || echo "kg-sync-sessions: $src keep-setting FAILED"
}

# --- 1+2) archive -------------------------------------------------------------
mkdir -p "$ARCH/ARES"
rsync -a /root/.claude/projects/ "$ARCH/ARES/" && echo "kg-sync-sessions: ARES archived"
python3 -c "$(keep_py /root/.claude/settings.json)"

LXC_PID=$(lxc-info -n 101 -p -H 2>/dev/null)
if [ -n "$LXC_PID" ] && [ -d "/proc/$LXC_PID/root/root/.claude/projects" ]; then
  mkdir -p "$ARCH/ARES-LXC101"
  rsync -a "/proc/$LXC_PID/root/root/.claude/projects/" "$ARCH/ARES-LXC101/" \
    && echo "kg-sync-sessions: ARES-LXC101 archived"
  pct exec 101 -- python3 -c "$(keep_py /root/.claude/settings.json)"
fi

if pull ZEUS-root "$SSH" $ZEUS /root/.claude/projects /root/.claude/settings.json; then
  pull ZEUS-zain "$SSH" $ZEUS /home/zain/.claude/projects /home/zain/.claude/settings.json \
    && timeout 20 $SSH $ZEUS "chown zain:zain /home/zain/.claude/settings.json"
fi
pull EROS "$SSH" root@eros /root/.claude/projects /root/.claude/settings.json
pull MAC-air "$SSH" zainkhatri@100.95.208.65 '~/.claude/projects' '~/.claude/settings.json'
pull MAC-prometheon "$SSH" zainkhatri@100.103.57.77 '~/.claude/projects' '~/.claude/settings.json'

# --- 3) index + summarize -----------------------------------------------------
for dir in "$ARCH"/*/; do
  src=$(basename "$dir")
  box=${src%%-*}                                      # ARES-LXC101 -> ARES, ZEUS-zain -> ZEUS
  env KG_DB="$DB" OLLAMA_HOST=http://127.0.0.1:11434 PYTHONPATH="$ATLAS" \
    python3 -m atlas.cli index-chats --box "$box" --root "$dir" --summary-budget "$BUDGET" \
    | sed "s/^/kg-sync-sessions: $src /" || echo "kg-sync-sessions: $src index FAILED"
done

echo "=== kg-sync-sessions done $(date -Is) ==="
