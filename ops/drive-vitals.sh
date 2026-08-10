#!/usr/bin/env bash
# ARES dashboard: physical NVMe vitals (990 PRO + 970 EVO).
# Runs on the Proxmox HOST — device temps and true filesystem usage are not
# visible inside LXC 101, so the dashboard reads the JSON this writes.
#   970 EVO = nvme1n1 = /mnt/nvme (the PROMETHEUS pool)
#   990 PRO = nvme0n1 = /          (Proxmox root / system + VM thin-pool)
set -eu

OUT=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.host_drives.json
TMP="$OUT.tmp.$$"

temp_c() {  # $1 = nvme0|nvme1 -> integer Celsius (0 if unreadable)
    local f; f=$(ls /sys/class/nvme/"$1"/hwmon*/temp1_input 2>/dev/null | head -1) || true
    [ -n "${f:-}" ] && awk '{printf "%d", ($1+500)/1000}' "$f" 2>/dev/null || echo 0
}

usage() {  # $1 = mountpoint -> "usedBytes totalBytes percent"
    df -B1 --output=used,size "$1" 2>/dev/null \
        | awk 'NR==2{printf "%d %d %d\n", $1, $2, ($2 ? $1*100/$2 : 0)}'
}

read -r evo_u evo_t evo_p < <(usage /mnt/nvme) || true
read -r pro_u pro_t pro_p < <(usage /) || true

cat > "$TMP" <<JSON
{"ts": $(date +%s), "drives": [
 {"name":"970-EVO","temp_c":$(temp_c nvme1),"used_bytes":${evo_u:-0},"total_bytes":${evo_t:-0},"percent":${evo_p:-0}},
 {"name":"990-PRO","temp_c":$(temp_c nvme0),"used_bytes":${pro_u:-0},"total_bytes":${pro_t:-0},"percent":${pro_p:-0}}
]}
JSON

mv -f "$TMP" "$OUT"
