#!/usr/bin/env bash
# Install proai-hermes-alerts (watchdog script + timer every 10 min). Idempotent. Run as root.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
install -m 755 "$HERE/proai-hermes-alerts.py" /usr/local/sbin/proai-hermes-alerts
install -d -m 700 /var/lib/proai-hermes-alerts
cat > /etc/systemd/system/proai-hermes-alerts.service <<'UNIT'
[Unit]
Description=ProAI Hermes Forge watchdog: Telegram alerts to the Forge admin on state changes
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /usr/local/sbin/proai-hermes-alerts
TimeoutStartSec=120
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/proai-hermes-alerts.timer <<'UNIT'
[Unit]
Description=Run the ProAI Hermes Forge watchdog every 10 minutes
[Timer]
OnBootSec=5min
OnCalendar=*:0/10
Persistent=false
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now proai-hermes-alerts.timer >/dev/null
systemctl list-timers --no-legend proai-hermes-alerts.timer
