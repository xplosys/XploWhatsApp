#!/bin/bash
# Disaster recovery: replace the live database with a backup. Run only through
# deploy/scripts/backup.sh restore <file>, which stops the app first and starts it after.
set -euo pipefail
GNUPGHOME=$(mktemp -d)
export GNUPGHOME
file="${1:?usage: restore.sh /backups/db/xplowhatsapp-<stamp>.dump.gpg}"
work=$(mktemp -d)
trap 'rm -rf "$work" "$GNUPGHOME"' EXIT
[ -s /keys/backup-private-key.asc ] || { echo "restore: the backup private key is not installed" >&2; exit 1; }
if [ -f "$file.sha256" ]; then (cd "$(dirname "$file")" && sha256sum --check --quiet "$(basename "$file").sha256"); fi
gpg --batch --quiet --import /keys/backup-private-key.asc
gpg --batch --quiet --decrypt --output "$work/db.dump" "$file"
pg_restore --list "$work/db.dump" > /dev/null
echo "restore: replacing $PGDATABASE with $(basename "$file")"
# Roles exist already (bootstrap-db); everything inside the database is replaced.
pg_restore --clean --if-exists --exit-on-error --single-transaction --no-password -d "$PGDATABASE" "$work/db.dump"
echo "restore: done"
