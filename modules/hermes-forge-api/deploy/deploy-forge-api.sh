#!/usr/bin/env bash
# Deploy hardened Forge API/control/bridge (+ edge nginx) from the worktree to prod, with full rollback on failure.
# Run as root on the VPS: bash deploy-forge-api.sh [rollback <backup-dir>]
set -uo pipefail
WT=/opt/vektor-dev/worktrees/forge-manager-20261023/modules/hermes-forge-api
P=/opt/proai-hermes-forge-api
EDGE=/opt/proai-hermes-forge-edge/nginx.conf
UNITS=(proai-hermes-forge-control proai-hermes-forge-api proai-hermes-forge-bridge)
TS=$(date +%Y%m%dT%H%M%S)
BK=/root/forge-fix/backup-forge-$TS
SOCK=/run/proai-hermes-forge-api/api.sock

edge_healthz() { docker exec hermes-forge-edge wget -qO- -T 5 http://host.docker.internal:8650/healthz 2>&1; }

restart_chain() {
  systemctl daemon-reload
  # control -> api -> bridge are chained with Requires=/After=; one restart of control restarts the whole chain.
  systemctl restart proai-hermes-forge-control.service
  sleep 3
}

diagnostics() {
  echo "== diagnostics"
  for u in "${UNITS[@]}"; do printf "%s: %s\n" "$u" "$(systemctl is-active $u.service)"; done
  ss -xlp 2>/dev/null | grep -F "$SOCK" | head -3
  ss -tlnp | grep 8650
  ls -la "$(dirname "$SOCK")" 2>&1 | tail -2
  echo "-- edge wget verbose:"; docker exec hermes-forge-edge wget -O- -S -T 5 http://host.docker.internal:8650/healthz 2>&1 | head -8
  echo "-- bridge journal:"; journalctl -u proai-hermes-forge-bridge.service --since -3min --no-pager -q -o cat | tail -12
  echo "-- api journal:"; journalctl -u proai-hermes-forge-api.service --since -3min --no-pager -q -o cat | tail -6
}

rollback() {
  local from=$1
  echo "!! ROLLBACK from $from"
  for f in api.py control.py static/app.js; do install -m 644 "$from/$f" "$P/$f"; done
  for u in "${UNITS[@]}"; do install -m 644 "$from/$u.service" "/etc/systemd/system/$u.service"; done
  [ -f "$from/edge-nginx.conf" ] && install -m 644 "$from/edge-nginx.conf" "$EDGE" && docker exec hermes-forge-edge nginx -s reload >/dev/null 2>&1
  restart_chain
  for u in "${UNITS[@]}"; do printf "%s: %s\n" "$u" "$(systemctl is-active $u.service)"; done
  echo "edge healthz after rollback: $(edge_healthz)"
}

if [ "${1:-}" = "rollback" ]; then rollback "$2"; exit $?; fi

install -d -m 700 "$BK/static"
for f in api.py control.py static/app.js; do cp -p "$P/$f" "$BK/$f"; done
for u in "${UNITS[@]}"; do cp -p "/etc/systemd/system/$u.service" "$BK/$u.service"; done
cp -p "$EDGE" "$BK/edge-nginx.conf"
echo "backup: $BK"

# 1) code
for f in api.py control.py static/app.js; do install -m 644 "$WT/$f" "$P/$f"; done
python3 -m py_compile "$P/api.py" "$P/control.py" || { rollback "$BK"; exit 1; }
# 2) units (api keeps its zoom-webhook drop-in from /etc/systemd/system/proai-hermes-forge-api.service.d/)
for u in "${UNITS[@]}"; do install -m 644 "$WT/deploy/$u.service" "/etc/systemd/system/$u.service"; done
if [ "${BRIDGE_DEBUG:-0}" = 1 ]; then sed -i 's|/usr/bin/socat |/usr/bin/socat -d -d |' /etc/systemd/system/proai-hermes-forge-bridge.service; fi
restart_chain
ok=1
for u in "${UNITS[@]}"; do st=$(systemctl is-active $u.service); printf "%s: %s\n" "$u" "$st"; [ "$st" = active ] || ok=0; done
H1=$(curl -s -m 5 --unix-socket "$SOCK" http://localhost/healthz); echo "socket healthz: $H1"
echo "$H1" | grep -q '"ok":true' || ok=0
H2=""
for i in 1 2 3 4; do H2=$(edge_healthz); echo "bridge healthz via edge (try $i): $H2"; echo "$H2" | grep -q '"ok":true' && break; sleep 4; done
echo "$H2" | grep -q '"ok":true' || ok=0
ss -tlnp | grep -q "127.0.0.1:8650" && echo "note: something still listens on 127.0.0.1:8650"
if [ "$ok" != 1 ]; then diagnostics; rollback "$BK"; exit 1; fi

# 3) edge nginx (real_ip + per-route limits)
install -m 644 "$WT/deploy/edge-nginx.conf" "$EDGE"
if docker exec hermes-forge-edge nginx -t >/dev/null 2>&1 && docker exec hermes-forge-edge nginx -s reload >/dev/null 2>&1; then
  sleep 1
  H3=$(curl -s -m 8 https://forge.srv1250550.hstgr.cloud/healthz); echo "public healthz: $H3"
  echo "$H3" | grep -q '"ok":true' || ok=0
else
  echo "edge nginx config rejected"; ok=0
fi
if [ "$ok" != 1 ]; then diagnostics; rollback "$BK"; exit 1; fi

# 4) smoke through the socket as the admin (read-only ops)
python3 -I /root/forge-fix/forge_smoke.py "unix:$SOCK" pavel || { diagnostics; rollback "$BK"; exit 1; }
journalctl -u proai-hermes-forge-control.service -u proai-hermes-forge-api.service --since -2min --no-pager -q -o cat | grep -i "traceback\|error" | tail -5
echo "DEPLOY OK (backup $BK)"
