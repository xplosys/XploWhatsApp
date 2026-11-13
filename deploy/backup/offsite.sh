#!/bin/sh
# Copies encrypted database backups (and, with self-hosted storage, the file bucket) to
# off-site S3-compatible storage. Runs in the rclone image with .env as its environment:
#   BACKUP_S3_ENDPOINT, BACKUP_S3_REGION, BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY,
#   BACKUP_S3_BUCKET, BACKUP_OFFSITE_KEEP_DAYS (default 35), BACKUP_OBJECTS (true to copy files)
set -eu
if [ -z "${BACKUP_S3_BUCKET:-}" ]; then
  echo "offsite: BACKUP_S3_BUCKET is not set; nothing copied (docs/DEPLOYMENT_UBUNTU.md, Backups)" >&2
  exit 1
fi
export RCLONE_CONFIG_OFFSITE_TYPE=s3
export RCLONE_CONFIG_OFFSITE_PROVIDER=Other
export RCLONE_CONFIG_OFFSITE_ENDPOINT="${BACKUP_S3_ENDPOINT:?}"
export RCLONE_CONFIG_OFFSITE_REGION="${BACKUP_S3_REGION:-auto}"
export RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="${BACKUP_S3_ACCESS_KEY_ID:?}"
export RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="${BACKUP_S3_SECRET_ACCESS_KEY:?}"
export RCLONE_CONFIG_OFFSITE_NO_CHECK_BUCKET=true

rclone copy /backups/db "offsite:$BACKUP_S3_BUCKET/db" --include 'xplowhatsapp-*' --immutable
rclone delete "offsite:$BACKUP_S3_BUCKET/db" --min-age "${BACKUP_OFFSITE_KEEP_DAYS:-35}d"
echo "offsite: database backups copied"

if [ "${BACKUP_OBJECTS:-false}" = "true" ]; then
  # Self-hosted SeaweedFS bucket (knowledge files, exports). Copy only; nothing is deleted
  # off-site, so a mistaken delete here can be undone from the copy.
  export RCLONE_CONFIG_LOCALS3_TYPE=s3
  export RCLONE_CONFIG_LOCALS3_PROVIDER=SeaweedFS
  export RCLONE_CONFIG_LOCALS3_ENDPOINT=http://seaweedfs:8333
  export RCLONE_CONFIG_LOCALS3_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID:?}"
  export RCLONE_CONFIG_LOCALS3_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY:?}"
  rclone copy "locals3:${S3_BUCKET:-xplowhatsapp-private}" "offsite:$BACKUP_S3_BUCKET/objects"
  echo "offsite: stored files copied"
fi

tmp=/backups/status/.offsite.$$
printf '# HELP xplo_backup_offsite_last_success_timestamp_seconds When backups were last copied off-site.\n# TYPE xplo_backup_offsite_last_success_timestamp_seconds gauge\nxplo_backup_offsite_last_success_timestamp_seconds %s\n' "$(date +%s)" > "$tmp"
chmod 644 "$tmp"
mv "$tmp" /backups/status/offsite.prom
