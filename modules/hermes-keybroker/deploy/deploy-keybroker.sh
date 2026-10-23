#!/usr/bin/env bash
# Deploy / update proai-keybroker on the VPS (run as root). Idempotent.
# Source: /opt/vektor-dev/keybroker (agy-worker output, reviewed). Inputs next to this script: keybroker-routes.json, lanectl.py.
set -euo pipefail
SRC=/opt/vektor-dev/keybroker
DST=/opt/proai-keybroker
ETC=/etc/proai-keybroker
HERE=$(cd "$(dirname "$0")" && pwd)
SVC_USER=proai-keybroker

id -u $SVC_USER >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/$SVC_USER --no-create-home $SVC_USER
install -d -m 755 $DST $ETC
install -d -m 750 -o $SVC_USER -g $SVC_USER /var/lib/$SVC_USER
rsync -a --delete --exclude __pycache__ "$SRC/keybroker/" "$DST/keybroker/"
install -m 644 "$SRC/README.md" "$DST/README.md"
if [ ! -x $DST/.venv/bin/python ]; then uv venv -q $DST/.venv --python 3.12; fi
uv pip install -q --python $DST/.venv/bin/python "aiohttp==3.14.5"
chown -R root:root $DST
ROUTES_SRC="$HERE/routes.json"; [ -f "$ROUTES_SRC" ] || ROUTES_SRC="$HERE/keybroker-routes.json"
install -m 640 -o root -g $SVC_USER "$ROUTES_SRC" $ETC/routes.json
[ -f $ETC/lanes.json ] || printf '{"lanes": []}\n' > $ETC/lanes.json
chown root:$SVC_USER $ETC/lanes.json; chmod 640 $ETC/lanes.json   # lanes/limits are not for other local users
install -m 755 "$HERE/lanectl.py" /usr/local/sbin/lanectl

# Oneshot WITHOUT RemainAfterExit: the timer must really re-run it (an active oneshot is a no-op for `start`).
cat > /etc/systemd/system/proai-keybroker-lanes.service <<'UNIT'
[Unit]
Description=ProAI key broker: per-client lane firewall rules (iptables owner match)
After=network-pre.target ufw.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lanectl apply
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/proai-keybroker-lanes.timer <<'UNIT'
[Unit]
Description=Re-apply key broker lane rules (survives ufw reloads)
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/proai-keybroker.service <<UNIT
[Unit]
Description=ProAI key broker for Hermes client bots (platform keys stay here; clients get per-uid lanes)
Wants=proai-keybroker-lanes.service network-online.target
After=network-online.target proai-keybroker-lanes.service
StartLimitIntervalSec=0
[Service]
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=/etc/proai-hermes-platform.env
Environment=KEYBROKER_LANES=$ETC/lanes.json KEYBROKER_ROUTES=$ETC/routes.json KEYBROKER_STATE_DIR=/var/lib/$SVC_USER KEYBROKER_ADMIN_PORT=21000 KEYBROKER_BIND=127.0.0.1
WorkingDirectory=$DST
ExecStartPre=+/usr/local/sbin/lanectl apply
ExecStart=$DST/.venv/bin/python -m keybroker
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=2
TimeoutStopSec=60
LimitNOFILE=65536
TasksMax=512
MemoryMax=1G
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6
StateDirectory=$SVC_USER
StateDirectoryMode=0750
ReadWritePaths=/var/lib/$SVC_USER
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now proai-keybroker-lanes.service proai-keybroker-lanes.timer >/dev/null
systemctl enable proai-keybroker.service >/dev/null
systemctl restart proai-keybroker.service
sleep 2
systemctl is-active proai-keybroker.service
curl -s -m 5 http://127.0.0.1:21000/healthz; echo
journalctl -u proai-keybroker.service --since -30s --no-pager -q | tail -5
