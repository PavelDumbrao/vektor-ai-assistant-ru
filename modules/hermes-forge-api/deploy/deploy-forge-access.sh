#!/usr/bin/env bash
# Deploy the "access requests + support + billing + purge" slice from a git ref (default: origin/main) to prod.
# Manager: manager.py i18n.py support.py offboard_purge.py docs/ ; Forge API: billing.py api.py control.py (+ billing drop-in).
# Writes the deployed commit SHA to DEPLOYED files. Rollback: bash deploy-forge-access.sh rollback <backup-dir>
set -uo pipefail
REPO=/opt/vektor-dev/vektor-ai-assistant-ru
REF=${REF:-origin/main}
PM=/opt/proai-hermes-manager
PA=/opt/proai-hermes-forge-api
TS=$(date +%Y%m%dT%H%M%S)
BK=/root/forge-fix/backup-access-$TS
MANAGER_FILES=(manager.py i18n.py support.py offboard_purge.py)
API_FILES=(billing.py api.py control.py)
SOCK=/run/proai-hermes-forge-api/api.sock

edge_healthz() { docker exec hermes-forge-edge wget -qO- -T 5 http://host.docker.internal:8650/healthz 2>&1; }

restart_all() {
  systemctl daemon-reload
  systemctl restart proai-hermes-manager.service
  systemctl restart proai-hermes-forge-control.service   # cascades to api + bridge via Requires=
  sleep 4
}

rollback() {
  local from=$1
  echo "!! ROLLBACK from $from"
  for f in "${MANAGER_FILES[@]}"; do [ -f "$from/manager/$f" ] && install -m 644 "$from/manager/$f" "$PM/$f"; done
  for f in "${API_FILES[@]}"; do [ -f "$from/api/$f" ] && install -m 644 "$from/api/$f" "$PA/$f"; done
  [ -f "$from/manager/support.absent" ] && rm -f "$PM/support.py"
  [ -f "$from/manager/offboard_purge.absent" ] && rm -f "$PM/offboard_purge.py"
  [ -f "$from/api/billing.absent" ] && rm -f "$PA/billing.py"
  [ -f "$from/api/billing.conf.absent" ] && rm -f /etc/systemd/system/proai-hermes-forge-api.service.d/billing.conf
  restart_all
  for u in proai-hermes-manager proai-hermes-forge-control proai-hermes-forge-api proai-hermes-forge-bridge; do printf "%s: %s\n" "$u" "$(systemctl is-active $u.service)"; done
}

if [ "${1:-}" = "rollback" ]; then rollback "$2"; exit $?; fi

cd "$REPO" && git fetch -q origin && SHA=$(git rev-parse "$REF") || { echo "cannot resolve $REF"; exit 1; }
echo "deploying $REF = $SHA"
EXPORT=$(mktemp -d /root/forge-fix/export-XXXXXX)
git archive "$SHA" modules/hermes-bot-manager modules/hermes-forge-api | tar -x -C "$EXPORT"
SRC_M=$EXPORT/modules/hermes-bot-manager
SRC_A=$EXPORT/modules/hermes-forge-api

install -d -m 700 "$BK/manager" "$BK/api"
for f in "${MANAGER_FILES[@]}"; do if [ -f "$PM/$f" ]; then cp -p "$PM/$f" "$BK/manager/$f"; else touch "$BK/manager/${f%.py}.absent"; fi; done
for f in "${API_FILES[@]}"; do if [ -f "$PA/$f" ]; then cp -p "$PA/$f" "$BK/api/$f"; else touch "$BK/api/${f%.py}.absent"; fi; done
[ -f /etc/systemd/system/proai-hermes-forge-api.service.d/billing.conf ] || touch "$BK/api/billing.conf.absent"
echo "backup: $BK"

for f in "${MANAGER_FILES[@]}"; do install -m 644 "$SRC_M/$f" "$PM/$f"; done
install -d -m 755 "$PM/docs" && install -m 644 "$SRC_M"/docs/*.md "$PM/docs/"
for f in "${API_FILES[@]}"; do install -m 644 "$SRC_A/$f" "$PA/$f"; done
install -d -m 755 /etc/systemd/system/proai-hermes-forge-api.service.d
install -m 644 "$SRC_A/deploy/proai-hermes-forge-api.service.d-billing.conf" /etc/systemd/system/proai-hermes-forge-api.service.d/billing.conf
python3 -m py_compile "$PM/manager.py" "$PM/support.py" "$PM/offboard_purge.py" "$PA/billing.py" "$PA/api.py" "$PA/control.py" || { rollback "$BK"; exit 1; }
echo "$SHA $(date -u +%FT%TZ) $REF" > "$PM/DEPLOYED"; cp "$PM/DEPLOYED" "$PA/DEPLOYED"
rm -rf "$EXPORT"

restart_all
ok=1
for u in proai-hermes-manager proai-hermes-forge-control proai-hermes-forge-api proai-hermes-forge-bridge; do st=$(systemctl is-active $u.service); printf "%s: %s\n" "$u" "$st"; [ "$st" = active ] || ok=0; done
H1=$(curl -s -m 5 --unix-socket "$SOCK" http://localhost/healthz); echo "socket healthz: $H1"; echo "$H1" | grep -q '"ok":true' || ok=0
H2=""; for i in 1 2 3; do H2=$(edge_healthz); echo "$H2" | grep -q '"ok":true' && break; sleep 4; done; echo "edge healthz: $H2"; echo "$H2" | grep -q '"ok":true' || ok=0
H3=$(curl -s -m 8 https://forge.srv1250550.hstgr.cloud/healthz); echo "public healthz: $H3"; echo "$H3" | grep -q '"ok":true' || ok=0
W=$(curl -s -m 8 -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/x-www-form-urlencoded" --data "order_id=1&payment_status=fail" https://forge.srv1250550.hstgr.cloud/v1/webhooks/prodamus); echo "prodamus webhook without Sign -> $W (expect 401; 503 = billing.env missing)"
journalctl -u proai-hermes-manager.service --since -1min --no-pager -q -o cat | grep -i "traceback\|error" | tail -3
journalctl -u proai-hermes-forge-control.service -u proai-hermes-forge-api.service --since -1min --no-pager -q -o cat | grep -i "traceback\|error" | tail -3
if [ "$ok" != 1 ]; then rollback "$BK"; exit 1; fi
python3 -I /root/forge-fix/forge_smoke.py "unix:$SOCK" pavel | tail -2 || { rollback "$BK"; exit 1; }
echo "DEPLOY OK $SHA (backup $BK)"
