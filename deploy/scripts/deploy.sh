#!/bin/bash
# Deploys a release on the server: deploy/scripts/deploy.sh <image tag>
# Pulls the images, runs migrations, restarts the app, and checks it answers. If the
# check fails, it puts the previous release back. Migrations are written to be
# backward compatible for one release, so the previous code runs on the new schema.
set -euo pipefail
cd "$(dirname "$0")/../.."
tag="${1:?usage: deploy.sh <image tag, e.g. the git commit>}"
compose() { docker compose -f docker-compose.prod.yml --env-file .env "$@"; }
previous=$(grep -E '^IMAGE_TAG=' .env | cut -d= -f2- || true)

set_tag() {
  if grep -qE '^IMAGE_TAG=' .env; then sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$1/" .env; else echo "IMAGE_TAG=$1" >> .env; fi
}

healthy() {
  for _ in $(seq 1 30); do
    if compose exec -T api python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).status == 200 else 1)" 2>/dev/null \
      && compose exec -T nginx wget -q -O /dev/null http://frontend:3000/login 2>/dev/null; then
      return 0
    fi
    sleep 4
  done
  return 1
}

echo "deploy: $previous -> $tag"
set_tag "$tag"
compose pull api frontend
if [ -n "$previous" ]; then
  # A fresh dump before migrations change anything (skipped on the very first deploy).
  deploy/scripts/backup.sh nightly-db-only || { echo "deploy: backup before migrating failed; stopping" >&2; set_tag "$previous"; exit 1; }
fi
compose up -d --remove-orphans postgres redis clamav
compose run --rm migrate
compose up -d --remove-orphans

if healthy; then
  echo "deploy: $tag is live"
  docker image prune -f --filter "until=168h" > /dev/null
  exit 0
fi

echo "deploy: $tag failed its health check" >&2
if [ -n "$previous" ]; then
  echo "deploy: rolling back to $previous" >&2
  set_tag "$previous"
  compose up -d --remove-orphans
  healthy && echo "deploy: $previous restored" >&2
fi
exit 1
