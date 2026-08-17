#!/usr/bin/env bash
# ARES dashboard: physical NVMe vitals (990 PRO + 970 EVO).
# Runs on the Proxmox HOST — device temps and true per-drive usage are not
# visible inside LXC 101, so the dashboard reads the JSON this writes.
#   970 EVO = nvme1n1 = /mnt/nvme            (the PROMETHEUS pool — one big fs)
#   990 PRO = nvme0n1 = / + LVM thin-pool    (Proxmox root + the VM storage)
# Totals are the PHYSICAL device size (both are 1TB); the 990's usage is the
# root fs PLUS the LVM thin-pool's actual data (df / alone only sees the 94G root).
set -eu
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"   # cron has a minimal PATH; lvs/lsblk live in sbin

OUT=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/.host_drives.json
TMP="$OUT.tmp.$$"

temp_c() {  # $1 = nvme0|nvme1 -> integer Celsius (0 if unreadable)
    local f; f=$(ls /sys/class/nvme/"$1"/hwmon*/temp1_input 2>/dev/null | head -1) || true
    [ -n "${f:-}" ] && awk '{printf "%d", ($1+500)/1000}' "$f" 2>/dev/null || echo 0
}

phys_bytes() {  # $1 = /dev/nvmeXn1 -> physical device size in bytes (0 if unknown)
    lsblk -bdno SIZE "$1" 2>/dev/null | head -1 || echo 0
}

df_used() {  # $1 = mountpoint -> used bytes (0 if unreadable)
    df -B1 --output=used "$1" 2>/dev/null | awk 'NR==2{print $1}' || echo 0
}

# LVM thin-pool actual data usage in bytes (the VMs live here on the 990).
thin_used() {  # -> bytes of real data in pve/data (0 if no LVM)
    local pct sz
    pct=$(lvs --noheadings -o data_percent pve/data 2>/dev/null | tr -d ' %') || pct=0
    sz=$(lvs --noheadings --units b --nosuffix -o lv_size pve/data 2>/dev/null | tr -d ' ') || sz=0
    awk "BEGIN{printf \"%d\", ${sz:-0}*${pct:-0}/100}"
}

pct() { awk "BEGIN{t=${2:-0}; printf \"%d\", (t? ${1:-0}*100/t : 0)}"; }

evo_t=$(phys_bytes /dev/nvme1n1); evo_u=$(df_used /mnt/nvme)
pro_t=$(phys_bytes /dev/nvme0n1); pro_u=$(( $(df_used /) + $(thin_used) ))

cat > "$TMP" <<JSON
{"ts": $(date +%s), "drives": [
 {"name":"970-EVO","temp_c":$(temp_c nvme1),"used_bytes":${evo_u:-0},"total_bytes":${evo_t:-0},"percent":$(pct "${evo_u:-0}" "${evo_t:-0}")},
 {"name":"990-PRO","temp_c":$(temp_c nvme0),"used_bytes":${pro_u:-0},"total_bytes":${pro_t:-0},"percent":$(pct "${pro_u:-0}" "${pro_t:-0}")}
]}
JSON

mv -f "$TMP" "$OUT"

# Scheduled-job status has no cron of its own — refresh it here (this script runs
# every minute), so the dashboard's "next run" times never go stale.
python3 /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ops/cron-status.py >/dev/null 2>&1 || true

# GPU vitals from the Windows gaming VM (via guest agent) when the 3080 is loaned there.
python3 /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ops/gpu-windows.py >/dev/null 2>&1 || true
