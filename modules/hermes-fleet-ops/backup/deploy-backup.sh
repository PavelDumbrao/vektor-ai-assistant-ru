#!/usr/bin/env bash
# Install proai-hermes-backup (script + oneshot service + nightly timer) on the VPS. Idempotent. Run as root.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
install -m 755 "$HERE/proai-hermes-backup.sh" /usr/local/sbin/proai-hermes-backup
install -d -m 700 /var/backups/proai-hermes
cat > /etc/systemd/system/proai-hermes-backup.service <<'UNIT'
[Unit]
Description=ProAI Hermes Forge: nightly backup of client profiles, their Postgres DBs and manager state
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/proai-hermes-backup
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=2h
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/proai-hermes-backup.timer <<'UNIT'
[Unit]
Description=Nightly ProAI Hermes Forge backup
[Timer]
OnCalendar=*-*-* 03:10:00
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now proai-hermes-backup.timer >/dev/null
systemctl list-timers --no-legend proai-hermes-backup.timer
