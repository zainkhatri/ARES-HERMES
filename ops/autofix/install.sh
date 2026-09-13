#!/bin/bash
# One-shot installer. Does NOT enable/start the watcher timer or apply
# service -- that is a deliberate manual step after the spec's Testing Plan
# (replay of today's real bugs through the pipeline) passes.
set -euo pipefail
REPO=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD

# Kill switch ships PRESENT (disabled) by default.
touch /root/ares-autofix-disabled
echo "kill switch created at /root/ares-autofix-disabled (pipeline is INERT until you delete this file)"

# apply.py gets its own auth token, independent of ares-shell-ctl's.
TOKEN_FILE="$REPO/ops/autofix/.apply-token"
if [ ! -f "$TOKEN_FILE" ]; then
  python3 -c "import secrets; print(secrets.token_hex(32))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "generated apply.py auth token at $TOKEN_FILE (chmod 600)"
fi

cat > /etc/systemd/system/ares-autofix-watcher.service <<'EOF'
[Unit]
Description=ARES autonomous-fixer watcher (polls failure sources, sole status-writer)
[Service]
Type=oneshot
WorkingDirectory=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
ExecStart=/usr/bin/python3 /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ops/autofix/watcher.py
EOF

cat > /etc/systemd/system/ares-autofix-watcher.timer <<'EOF'
[Unit]
Description=Run ares-autofix-watcher once a day
[Timer]
OnCalendar=*-*-* 05:00:00
Persistent=true
[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/ares-autofix-apply.service <<'EOF'
[Unit]
Description=ARES autonomous-fixer privileged apply service (own auth, own port)
After=network.target
[Service]
Type=simple
WorkingDirectory=/mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD
ExecStart=/usr/bin/python3 /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD/ops/autofix/apply.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "installed. NOT enabled/started -- enable ares-autofix-apply.service manually when ready,"
echo "and only remove /root/ares-autofix-disabled after the spec's replay-test graduation criteria pass."
