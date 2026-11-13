#!/bin/bash
# Nightly database backup. Runs in the Postgres image (same pg_dump version as the server).
# The dump is encrypted to XploSys's backup public key, so a copy that leaks off-site is
# unreadable without the private key, which is kept off this server's backup path.
set -euo pipefail
GNUPGHOME=$(mktemp -d)
export GNUPGHOME
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dir=/backups/db
mkdir -p "$dir" /backups/status
out="$dir/xplowhatsapp-$stamp.dump.gpg"
partial="$dir/.xplowhatsapp-$stamp.partial"
trap 'rm -f "$partial"; rm -rf "$GNUPGHOME"' EXIT

if [ ! -s /keys/backup-public-key.asc ]; then
  echo "backup: deploy/secrets/backup-public-key.asc is missing (docs/DEPLOYMENT_UBUNTU.md, Backups)" >&2
  exit 1
fi

echo "backup: dumping $PGDATABASE"
pg_dump --format=custom --compress=6 --no-password \
  | gpg --batch --yes --quiet --trust-model always --compress-algo none \
        --recipient-file /keys/backup-public-key.asc --encrypt --output "$partial"
mv "$partial" "$out"
(cd "$dir" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
size=$(stat -c %s "$out")
echo "backup: wrote $(basename "$out") ($size bytes)"

find "$dir" -name 'xplowhatsapp-*.dump.gpg*' -mtime +"${BACKUP_KEEP_DAYS:-14}" -print -delete

# For node-exporter's textfile collector: the BackupMissing alert reads this.
tmp=$(mktemp /backups/status/.backup.XXXXXX)
cat > "$tmp" <<METRICS
# HELP xplo_backup_last_success_timestamp_seconds When the last database backup finished.
# TYPE xplo_backup_last_success_timestamp_seconds gauge
xplo_backup_last_success_timestamp_seconds $(date +%s)
# HELP xplo_backup_last_size_bytes Size of the last encrypted database backup.
# TYPE xplo_backup_last_size_bytes gauge
xplo_backup_last_size_bytes $size
METRICS
chmod 644 "$tmp"
mv "$tmp" /backups/status/backup.prom
