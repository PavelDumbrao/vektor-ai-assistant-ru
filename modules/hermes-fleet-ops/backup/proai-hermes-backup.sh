#!/usr/bin/env bash
# proai-hermes-backup: nightly consistent backup of every Forge client (sqlite state.db via .backup,
# ~/.hermes tarball, per-client Postgres DB) plus manager state, profiles, fleet policy and broker lanes.
# Runs as root from proai-hermes-backup.timer. Destination is root-only (contains client bot tokens).
# External/off-site copy is intentionally NOT done here (destination is Pavel's decision).
set -uo pipefail
umask 077

DEST_ROOT=${DEST_ROOT:-/var/backups/proai-hermes}
KEEP_DAYS=${KEEP_DAYS:-7}
PROFILES=${PROFILES:-/opt/vektor/profiles}
MANAGER_ROOT=${MANAGER_ROOT:-/opt/proai-hermes-manager}
PG_CONTAINER=${PG_CONTAINER:-passive-secretary-postgres}
STAMP=$(date +%Y-%m-%d)
DEST="$DEST_ROOT/$STAMP"
TMP="$DEST.tmp"
fail=0

log() { printf '%s backup: %s\n' "$(date -u +%H:%M:%S)" "$*"; }
# Runs a libpq client inside the Postgres container; credentials come from the container's own env, never from here.
pg() { docker exec "$PG_CONTAINER" sh -c 'export PGPASSWORD="$POSTGRES_PASSWORD" PGUSER="$POSTGRES_USER" PGHOST=localhost; exec "$@"' sh "$@"; }

install -d -m 700 "$DEST_ROOT"
rm -rf "$TMP"
install -d -m 700 "$TMP"

backup_client() {
  local owner=$1 home hermes out db
  home=$(getent passwd "$owner" | cut -d: -f6) || return 1
  hermes="$home/.hermes"
  if [ ! -d "$hermes" ] || [ -L "$hermes" ]; then log "skip $owner: no ~/.hermes directory"; return 0; fi
  out="$TMP/$owner"; install -d -m 700 "$out"
  # 1) sqlite: consistent copy through the backup API (client-owned path: never follow symlinks)
  if [ -f "$hermes/state.db" ] && [ ! -L "$hermes/state.db" ]; then
    if ! sqlite3 "file:$hermes/state.db?mode=ro" ".backup '$out/state.db'"; then
      log "FAIL $owner: sqlite backup"; return 1
    fi
    zstd -q --rm -T2 "$out/state.db" || { log "FAIL $owner: zstd state.db"; return 1; }
  fi
  # 2) the rest of ~/.hermes (tar never follows symlinks; heavy/rebuildable data excluded)
  if ! tar -C "$hermes" \
        --exclude=./state.db --exclude=./state.db-wal --exclude=./state.db-shm \
        --exclude=./video_editor/jobs --exclude='*/__pycache__' --exclude=./.cache \
        --exclude=./venv --exclude=./node_modules --warning=no-file-changed \
        -cf - . | zstd -q -T2 -o "$out/hermes.tar.zst"; then
    log "FAIL $owner: tar"; return 1
  fi
  # 3) Postgres database of this client (passive secretary / living memory), if it exists
  db="hermes_$owner"
  if pg psql -d postgres -Atc "select 1 from pg_database where datname='$db'" 2>/dev/null | grep -qx 1; then
    if ! pg pg_dump -Fc "$db" > "$out/$db.dump"; then
      log "FAIL $owner: pg_dump $db"; return 1
    fi
  fi
  log "ok $owner ($(du -sh "$out" | cut -f1))"
}

for pj in "$PROFILES"/*.json; do
  [ -f "$pj" ] || continue
  owner=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["owner"])' "$pj") || { log "FAIL: bad profile $pj"; fail=1; continue; }
  case "$owner" in *[!a-z0-9_-]*|"") log "FAIL: bad owner in $pj"; fail=1; continue;; esac
  backup_client "$owner" || fail=1
done

# Manager state (state.json, instances, receipts, managed/*.env bot tokens), profiles, policy, broker lanes/routes.
tar -C "$MANAGER_ROOT" -cf - state | zstd -q -o "$TMP/manager-state.tar.zst" || { log "FAIL: manager state"; fail=1; }
cp -a "$PROFILES" "$TMP/profiles" || fail=1
cp /etc/proai-hermes-fleet-policy.json "$TMP/" 2>/dev/null || log "warn: no fleet policy file"
install -d -m 700 "$TMP/keybroker"
cp /etc/proai-keybroker/lanes.json /etc/proai-keybroker/routes.json "$TMP/keybroker/" 2>/dev/null || log "warn: keybroker config missing"
pg pg_dumpall --globals-only > "$TMP/pg-globals.sql" 2>/dev/null || log "warn: pg globals"

rm -rf "$DEST"
mv "$TMP" "$DEST"
find "$DEST_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +"$KEEP_DAYS" -exec rm -rf {} +

log "done -> $DEST ($(du -sh "$DEST" | cut -f1)); free: $(df -h "$DEST_ROOT" | awk 'NR==2{print $4}')"
if [ "$fail" -eq 0 ]; then
  date -u +%s > "$DEST_ROOT/last-ok"
  exit 0
fi
log "finished WITH FAILURES (last-ok not updated)"
exit 1
