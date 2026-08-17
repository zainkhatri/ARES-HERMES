#!/usr/bin/env bash
# Sync the dashboard CODE to ZEUS (the unified "one codebase, two deployments"
# setup). Runs from ARES. Ships code+templates+small static assets ONLY — never
# the photo library, media caches, ML venvs, vault, or runtime state.
#
# The excludes are load-bearing: an earlier run that missed them copied 200G+ of
# static/video_cache + hls + a 5G clip venv. Keep this list authoritative.
#
# Usage:  ./deploy/sync-to-zeus.sh [--restart]
#   --restart  also restart the zeus-dashboard user service after syncing.
set -euo pipefail

SRC="/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/"
DEST="zain@100.100.29.36:/home/zain/ARES-DASHBOARD/"

# Heavy / host-specific / secret paths that must never leave ARES.
EXCLUDES=(
  --exclude='.venv'                 # per-box venv; ZEUS builds its own
  --exclude='clip-gpu-venv'         # 5G CLIP/torch venv — photo-only
  --exclude='ai_data'               # embeddings, face clusters, vault_auth
  --exclude='vault_enc' --exclude='vault_*'   # My Eyes Only encrypted originals
  --exclude='static/video_cache'    # transcoded video (tens–hundreds of GB)
  --exclude='static/hls'            # HLS segments
  --exclude='static/thumbs*'        # every thumbnail tier
  --exclude='static/faces'          # face crops
  --exclude='photo_index*'          # photo DB/json — ARES only
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm'
  --exclude='.host_*.json'          # host-collector runtime state (per box)
  --exclude='.folder_sizes.json'    # per-box folder-size cache (slow du — must persist)
  --exclude='.disk_stats.json' --exclude='.nas_drives.json'   # per-box disk caches
  --exclude='.env'                  # secrets stay per box
  --exclude='__pycache__' --exclude='*.pyc'
  --exclude='.git'
)

echo "→ syncing code to ZEUS (code+templates+small static only)…"
rsync -az --delete "${EXCLUDES[@]}" "$SRC" "$DEST"

# Stamp the remote with the source git SHA so /healthz can prove no drift.
STAMP=$(git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD rev-parse --short HEAD 2>/dev/null || echo dev)
ssh zain@100.100.29.36 "printf '%s' '$STAMP' > ~/ARES-DASHBOARD/.deploy_stamp"

# Safety net: code tree is tens of MB (+~100MB venv). Fail on BOTH a blowup
# (leaked heavy data) AND a collapse — a bad exclude or empty SRC that --delete
# would otherwise silently propagate, wiping the remote.
SIZE=$(ssh zain@100.100.29.36 'du -sm ~/ARES-DASHBOARD 2>/dev/null | cut -f1')
echo "→ remote ARES-DASHBOARD is ${SIZE} MB (stamp ${STAMP})"
if [ "${SIZE:-0}" -gt 500 ]; then
  echo "!! remote >500MB — an exclude likely leaked heavy data. Investigate." >&2; exit 1
fi
if [ "${SIZE:-0}" -lt 5 ]; then
  echo "!! remote <5MB — sync looks empty/collapsed. Aborting before it spreads." >&2; exit 1
fi

if [ "${1:-}" = "--restart" ]; then
  echo "→ restarting zeus-dashboard user service…"
  ssh zain@100.100.29.36 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart zeus-dashboard && sleep 4 && systemctl --user is-active zeus-dashboard'
  # Drift check: the running box must report the stamp we just pushed.
  REMOTE=$(ssh zain@100.100.29.36 'curl -s http://100.100.29.36:8890/healthz' | grep -o '"stamp":"[^"]*"' | cut -d'"' -f4)
  if [ "$REMOTE" = "$STAMP" ]; then echo "✓ /healthz stamp matches ($STAMP) — no drift"; else
    echo "!! /healthz stamp=$REMOTE != pushed $STAMP — restart/drift problem" >&2; exit 1; fi
fi
echo "✓ done"
