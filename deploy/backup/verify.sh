#!/bin/bash
# Weekly restore check: decrypt the newest backup, restore it into a scratch database on
# the same server, compare it with the live database, then drop the scratch copy.
# Needs the backup private key at /keys/backup-private-key.asc.
set -euo pipefail
GNUPGHOME=$(mktemp -d)
export GNUPGHOME
scratch=xplo_restore_check
work=$(mktemp -d)
cleanup() {
  psql -q -d postgres -c "DROP DATABASE IF EXISTS $scratch" >/dev/null 2>&1 || true
  rm -rf "$work" "$GNUPGHOME"
}
trap cleanup EXIT

# shellcheck disable=SC2012  # our own file names, no spaces
latest=$(ls -1t /backups/db/xplowhatsapp-*.dump.gpg 2>/dev/null | head -1 || true)
[ -n "$latest" ] || { echo "verify: no backups in /backups/db" >&2; exit 1; }
[ -s /keys/backup-private-key.asc ] || { echo "verify: the backup private key is not installed" >&2; exit 1; }
(cd /backups/db && sha256sum --check --quiet "$(basename "$latest").sha256")
echo "verify: checksum ok for $(basename "$latest")"

gpg --batch --quiet --import /keys/backup-private-key.asc
gpg --batch --quiet --decrypt --output "$work/db.dump" "$latest"
pg_restore --list "$work/db.dump" > /dev/null

psql -q -d postgres -c "DROP DATABASE IF EXISTS $scratch" -c "CREATE DATABASE $scratch"
pg_restore --exit-on-error --no-password -d "$scratch" "$work/db.dump"

q() { psql -X -A -t -d "$1" -c "$2"; }
live_version=$(q "$PGDATABASE" "SELECT version_num FROM alembic_version")
restored_version=$(q "$scratch" "SELECT version_num FROM alembic_version")
tables=$(q "$scratch" "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
policies=$(q "$scratch" "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
tenants=$(q "$scratch" "SELECT count(*) FROM tenants")
echo "verify: schema $restored_version (live $live_version), $tables tables, $policies RLS policies, $tenants businesses"
[ "$tables" -gt 20 ] || { echo "verify: too few tables restored" >&2; exit 1; }
[ "$policies" -gt 10 ] || { echo "verify: row-level security policies are missing" >&2; exit 1; }

tmp=$(mktemp /backups/status/.verify.XXXXXX)
cat > "$tmp" <<METRICS
# HELP xplo_backup_last_verified_timestamp_seconds When a backup was last restored and checked.
# TYPE xplo_backup_last_verified_timestamp_seconds gauge
xplo_backup_last_verified_timestamp_seconds $(date +%s)
METRICS
chmod 644 "$tmp"
mv "$tmp" /backups/status/verify.prom
echo "verify: restore check passed"
