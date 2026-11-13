#!/bin/bash
# Backups on the server (the systemd timers in deploy/systemd call this):
#   backup.sh nightly           database dump, then the off-site copy
#   backup.sh nightly-db-only   database dump only (used before each deploy's migrations)
#   backup.sh verify            restore the newest dump into a scratch database and check it
#   backup.sh restore <file>    replace the live database with <file> (stops the app meanwhile)
set -euo pipefail
cd "$(dirname "$0")/../.."
compose() { docker compose -f docker-compose.prod.yml --env-file .env "$@"; }

case "${1:-}" in
  nightly)
    compose run --rm backup
    compose run --rm backup-offsite
    ;;
  nightly-db-only)
    compose run --rm backup
    ;;
  verify)
    compose run --rm --entrypoint /bin/bash backup /scripts/verify.sh
    ;;
  restore)
    file="${2:?usage: backup.sh restore backups/db/xplowhatsapp-<stamp>.dump.gpg}"
    name=$(basename "$file")
    [ -f "backups/db/$name" ] || { echo "No such backup: backups/db/$name" >&2; exit 1; }
    read -r -p "Replace the live database with $name? Type RESTORE to continue: " answer
    [ "$answer" = "RESTORE" ] || { echo "Cancelled."; exit 1; }
    compose stop nginx frontend api worker worker-ai beat
    compose run --rm --entrypoint /bin/bash backup /scripts/restore.sh "/backups/db/$name"
    compose run --rm migrate
    compose up -d
    echo "Restored $name. Check the app, then run: deploy/scripts/backup.sh nightly"
    ;;
  *)
    sed -n '2,7p' "$0"; exit 1 ;;
esac
