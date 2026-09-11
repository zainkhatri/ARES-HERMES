#!/bin/bash
# fleet-collect.sh — runs on the ARES HOST (root), not in the LXC.
# SSH-probes EROS + reads local backup status, writes ai_data/fleet.json which
# the dashboard (LXC) reads via /api/fleet. The LXC has no route to the peer
# IPs, so the host does the reaching. Mirrors the MOTD probe + the codebase's
# existing "read a JSON file on disk" pattern (/api/outreach, sidekick rollup).
set -u

OUT="/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ai_data/fleet.json"
TMP="${OUT}.tmp"
ERO_IP="10.0.1.69"
ZEUS_IP="100.100.29.36"
ARES_BK_STATUS="/var/log/ares-backup-to-hermes.status"   # RETIRED (old push); kept for reference
HORCRUX_STAMP="/mnt/nvme/PROMETHEUS/INFRA/status/zeus-backup-stamp"  # the real backup: ZEUS pulls ARES+EROS at 4am
ZEUS_SNAP_DIR="/mnt/nvme/PROMETHEUS/HERMES-SIDEKICK/snapshots"

now=$(date +%s)

# --- EROS: one SSH round-trip emitting key=value lines ---------------------
# 1s window samples BOTH cpu (from /proc/stat) and net (rx+tx bytes) so the
# rates are honest without a long-running call.
ero_probe='
  read _ a b c d e rest < /proc/stat; i1=$d; t1=$((a+b+c+d+e))
  rx1=$(cat /sys/class/net/*/statistics/rx_bytes | paste -sd+ | bc); tx1=$(cat /sys/class/net/*/statistics/tx_bytes | paste -sd+ | bc)
  sleep 1
  read _ a b c d e rest < /proc/stat; i2=$d; t2=$((a+b+c+d+e))
  rx2=$(cat /sys/class/net/*/statistics/rx_bytes | paste -sd+ | bc); tx2=$(cat /sys/class/net/*/statistics/tx_bytes | paste -sd+ | bc)
  dt=$((t2-t1)); di=$((i2-i1)); cpu=0; [ "$dt" -gt 0 ] && cpu=$(( (100*(dt-di))/dt ))
  echo "cpu_pct=$cpu"
  echo "net_rx_mbs=$(awk "BEGIN{printf \"%.2f\",($rx2-$rx1)/1048576}")"
  echo "net_tx_mbs=$(awk "BEGIN{printf \"%.2f\",($tx2-$tx1)/1048576}")"
  echo "load=$(cut -d" " -f1 /proc/loadavg)"
  echo "cores=$(nproc)"
  echo "cpu_model=$(awk -F: "/model name/{gsub(/^ +/,\"\",\$2);print \$2;exit}" /proc/cpuinfo | sed "s/([^)]*)//g;s/CPU//;s/  */ /g")"
  t=$(sensors -j 2>/dev/null | grep -oE "temp[0-9]+_input\": [0-9.]+" | grep -oE "[0-9.]+" | sort -rn | head -1)
  [ -n "$t" ] && echo "temp_c=${t%.*}"
  # GPU (GTX 1070): util, temp, mem used/total (MB), power (W)
  g=$(nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d " ")
  [ -n "$g" ] && echo "gpu=$g"
  free -m | awk "/^Mem:/{printf \"mem_used_mb=%d\nmem_total_mb=%d\n\",\$3,\$2}"
  df -P / | awk "NR==2{printf \"disk_used_kb=%d\ndisk_total_kb=%d\ndisk_pct=%d\n\",\$3,\$2,\$5}"
  echo "uptime=$(uptime -p 2>/dev/null | sed "s/^up //")"
  curl -s -m2 http://127.0.0.1:11434/api/tags 2>/dev/null | grep -oE "\"name\":\"[^\"]+\"" | sed "s/\"name\":\"//;s/\"//" | head -6 | while read -r m; do echo "model=$m"; done
  qm list 2>/dev/null | awk "NR>1{printf \"vm=%s|%s|%s\n\",\$1,\$2,\$3}"
  docker ps -a --format "svc={{.Names}}|{{.State}}" 2>/dev/null
'

ero_up=false; ero_raw=""
if ping -c1 -W1 "$ERO_IP" >/dev/null 2>&1; then
  ero_raw=$(timeout 14 ssh -o BatchMode=yes -o ConnectTimeout=8 \
              -o StrictHostKeyChecking=accept-new "root@${ERO_IP}" "$ero_probe" 2>/dev/null)
  [ -n "$ero_raw" ] && ero_up=true
fi

# Build the EROS JSON object with jq from the key=value lines (jq -R/-s parse).
ero_json='{"up":false}'
if [ "$ero_up" = true ]; then
  ero_json=$(printf '%s\n' "$ero_raw" | jq -Rn '
    reduce inputs as $l ({up:true, vms:[], models:[], containers:[]};
      ($l | capture("^(?<k>[a-z_]+)=(?<v>.*)$") // null) as $m
      | if   $m == null then .
        elif $m.k=="vm" then
          ($m.v | split("|")) as $p
          | .vms += [{id:($p[0]|tonumber? // $p[0]), name:$p[1], state:$p[2]}]
        elif $m.k=="svc" then
          ($m.v | split("|")) as $p
          | .containers += [{name:$p[0], state:$p[1]}]
        elif $m.k=="model" then .models += [$m.v]
        elif $m.k=="gpu" then
          ($m.v | split(",")) as $p
          | .gpu = {util:($p[0]|tonumber? // 0), temp:($p[1]|tonumber? // null),
                    mem_used:($p[2]|tonumber? // 0), mem_total:($p[3]|tonumber? // 0),
                    power:($p[4]|tonumber? // null)}
        else .[$m.k] = ($m.v|tonumber? // $m.v) end)
    | if .mem_used_mb  then .mem_used_gb=(.mem_used_mb/1024*10|round/10)   | del(.mem_used_mb)  else . end
    | if .mem_total_mb then .mem_total_gb=(.mem_total_mb/1024*10|round/10) | del(.mem_total_mb) else . end
    | if .disk_used_kb then .disk_used_gb=(.disk_used_kb/1048576|round)    | del(.disk_used_kb) else . end
    | if .disk_total_kb then .disk_total_gb=(.disk_total_kb/1048576|round) | del(.disk_total_kb) else . end
  ') || ero_json='{"up":true}'
fi

# --- /mnt/eros business tree (sshfs mount on ARES host) → folders + mount health ---
ero_mounted=false; ero_folders='[]'
if mountpoint -q /mnt/eros 2>/dev/null; then
  ero_mounted=true
  ero_folders=$(ls -1 /mnt/eros/BUSINESS/AUTOMATION-IBT 2>/dev/null \
                | grep -vE '^(archive|docs|CLAUDE\.md|website)$' | head -12 \
                | jq -Rn '[inputs]' 2>/dev/null)
  [ -z "$ero_folders" ] && ero_folders='[]'
fi
ero_json=$(printf '%s' "$ero_json" | jq --argjson f "$ero_folders" --argjson m "$ero_mounted" \
  '.mount = {path:"/mnt/eros", mounted:$m, folders:$f}' 2>/dev/null) || ero_json="$ero_json"

# --- Backups: read the HORCRUX stamp (ZEUS pulls ARES+EROS nightly at 04:00) --
# ares_to_zeus slot = ARES's data landed on ZEUS; zeus_to_ares slot repurposed = EROS's data on ZEUS.
# A stamp older than the nightly window is normal (ZEUS sleeps); the MOTD shows "Nh ago", not "failed".
a2z_state="UNKNOWN"; a2z_epoch=0; z2a_state="UNKNOWN"; z2a_epoch=0
if [ -r "$HORCRUX_STAMP" ]; then
  read hs hep hares heros _ < "$HORCRUX_STAMP"
  a2z_epoch="${hep:-0}"; z2a_epoch="${hep:-0}"
  [ "${hares#ares=}" = "1" ] && a2z_state="OK" || a2z_state="FAIL"
  [ "${heros#eros=}" = "1" ] && z2a_state="OK" || z2a_state="FAIL"
fi
snap_count=0; snap_oldest=""; snap_size=""

zeus_reach=false
ping -c1 -W1 "$ZEUS_IP" >/dev/null 2>&1 && zeus_reach=true

# ZEUS SSD health — only probeable when ZEUS is awake (nightly ~4am horcrux).
# Cache last-good so the panel shows the most recent wake's readings while asleep.
DISKS_CACHE="/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ai_data/zeus-disks.json"
zeus_disks='[]'; disks_epoch=0
if [ "$zeus_reach" = true ]; then
  dp=$(timeout 14 ssh -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new root@"$ZEUS_IP" '
    for d in $(lsblk -dno NAME,TYPE 2>/dev/null | awk "\$2==\"disk\"{print \$1}" | grep -E "^sd" | head -3); do
      h=$(smartctl -H -d sat /dev/$d 2>/dev/null | grep -ioE "PASSED|FAILED|OK" | head -1)
      A=$(smartctl -A -d sat /dev/$d 2>/dev/null)
      tp=$(echo "$A" | awk "/Temperature_Celsius|Airflow_Temperature_Cel/{print \$10; exit}")
      # SSD lifespan remaining %: prefer NVMe Percentage Used, else Wear_Leveling/Media_Wearout normalized VALUE (100=new)
      pu=$(smartctl -A -d sat /dev/$d 2>/dev/null | grep -iE "Percentage Used" | grep -oE "[0-9]+" | head -1)
      wl=$(echo "$A" | awk "/Wear_Leveling_Count/{print \$4; exit}")
      mw=$(echo "$A" | awk "/Media_Wearout_Indicator/{print \$4; exit}")
      life=""; if [ -n "$pu" ]; then life=$((100-10#$pu)); elif [ -n "$wl" ]; then life=$((10#$wl)); elif [ -n "$mw" ]; then life=$((10#$mw)); fi
      full=$(smartctl -i -d sat /dev/$d 2>/dev/null | awk -F: "/Device Model|Model Number/{gsub(/^ +/,\"\",\$2);print \$2;exit}")
      short=$(echo "$full" | grep -oiE "T[0-9]+" | head -1); [ -z "$short" ] && short=$(lsblk -dno MODEL /dev/$d 2>/dev/null | awk "{print \$1}")
      mp=$(lsblk -rno MOUNTPOINT /dev/$d 2>/dev/null | grep -E "/mnt|/srv" | head -1)
      us=$(df -P "${mp:-/}" 2>/dev/null | awk "NR==2{print \$5}" | tr -d %)
      echo "$d|${short:-$d}|${h:-?}|${tp:-?}|${us:-0}|${life:-}"
    done' 2>/dev/null)
  if [ -n "$dp" ]; then
    zeus_disks=$(printf '%s\n' "$dp" | jq -Rn '[inputs | select(length>0) | split("|") | {dev:.[0], name:.[1], health:.[2], temp:(.[3]|tonumber? // null), use:(.[4]|tonumber? // 0), life:(.[5]|tonumber? // null)}]')
    disks_epoch=$now
    printf '{"ts":%s,"disks":%s}\n' "$now" "$zeus_disks" > "$DISKS_CACHE"
  fi
fi
# fall back to last-good cache when this run had no fresh data (asleep)
if [ "$zeus_disks" = '[]' ] && [ -r "$DISKS_CACHE" ]; then
  zeus_disks=$(jq -c '.disks // []' "$DISKS_CACHE" 2>/dev/null || echo '[]')
  disks_epoch=$(jq -r '.ts // 0' "$DISKS_CACHE" 2>/dev/null || echo 0)
fi

# --- Daily trend logs (once/day) so ZEUS panel can PREDICT, not just show now --
# capacity: one row/day of each drive's %used → growth slope → days-to-full.
# pull: one row/day of the nightly horcrux result → 60-night backup heatmap.
today=$(date +%F)
CAP_LOG="/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ai_data/zeus-capacity-history.json"
PULL_LOG="/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ai_data/zeus-pull-history.json"
[ -s "$CAP_LOG" ]  || echo '[]' > "$CAP_LOG"
[ -s "$PULL_LOG" ] || echo '[]' > "$PULL_LOG"
# capacity: append today's per-drive use% if we have disk data and today not logged
if [ "$zeus_disks" != '[]' ] && ! jq -e --arg d "$today" 'any(.[]; .d==$d)' "$CAP_LOG" >/dev/null 2>&1; then
  jq --arg d "$today" --argjson disks "$zeus_disks" \
     '. + [{d:$d, drives:($disks|map({name:.name, use:.use}))}] | .[-60:]' \
     "$CAP_LOG" > "${CAP_LOG}.tmp" && mv -f "${CAP_LOG}.tmp" "$CAP_LOG"
fi
# pull: append today's horcrux outcome once
if ! jq -e --arg d "$today" 'any(.[]; .d==$d)' "$PULL_LOG" >/dev/null 2>&1; then
  jq --arg d "$today" --arg a "$a2z_state" --arg z "$z2a_state" \
     '. + [{d:$d, a2z:$a, z2a:$z}] | .[-60:]' \
     "$PULL_LOG" > "${PULL_LOG}.tmp" && mv -f "${PULL_LOG}.tmp" "$PULL_LOG"
fi
cap_hist=$(cat "$CAP_LOG"); pull_hist=$(cat "$PULL_LOG")

# --- Assemble + atomic write ----------------------------------------------
jq -n \
  --argjson ts "$now" \
  --argjson eros "$ero_json" \
  --arg a2z_state "$a2z_state" --argjson a2z_epoch "${a2z_epoch:-0}" \
  --arg z2a_state "$z2a_state" --argjson z2a_epoch "${z2a_epoch:-0}" \
  --argjson zeus_reach "$zeus_reach" \
  --argjson snap_count "${snap_count:-0}" \
  --arg snap_oldest "$snap_oldest" --arg snap_size "$snap_size" \
  --argjson disks "$zeus_disks" --argjson disks_epoch "${disks_epoch:-0}" \
  --argjson cap_hist "$cap_hist" --argjson pull_hist "$pull_hist" \
  '{ts:$ts, eros:$eros,
    backups:{
      ares_to_zeus:{state:$a2z_state, epoch:$a2z_epoch},
      zeus_to_ares:{state:$z2a_state, epoch:$z2a_epoch},
      zeus_reachable:$zeus_reach,
      snap_count:$snap_count, snap_oldest:$snap_oldest, snap_size:$snap_size,
      disks:$disks, disks_epoch:$disks_epoch,
      capacity_history:$cap_hist, pull_history:$pull_hist}}' > "$TMP" && mv -f "$TMP" "$OUT"
