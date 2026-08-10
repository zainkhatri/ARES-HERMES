#!/bin/bash
# index_videos.sh — Full pipeline: GPU frame extraction (VM 300) + CPU CLIP embedding (ARES LXC)
# Run this from the Proxmox host. Requires /mnt/nvme NFS-mounted on VM 300 at /mnt/nvme.
set -e

echo "[$(date)] Step 1: Extract video frames on VM 300 (GPU ffmpeg)"
ssh zain@192.168.20.212 "PYTHONUNBUFFERED=1 /opt/faceenv/bin/python3 -u /mnt/nvme/ares-app/scripts/extract_video_frames.py"

echo "[$(date)] Step 2: Embed frames via CLIP on ARES LXC (CPU)"
pct exec 101 -- /mnt/data/ares-app/.venv/bin/python3 /mnt/data/ares-app/scripts/embed_video_frames.py

echo "[$(date)] Step 3: Restart ARES Flask to reload index"
pct exec 101 -- systemctl restart ares
sleep 5
pct exec 101 -- journalctl -u ares --no-pager -n 5 | grep "\[ai\]"

echo "[$(date)] Done. Check /api/photos/search/status for updated count."
