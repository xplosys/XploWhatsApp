#!/bin/bash
# Writes .env for a throwaway test stack (CI, or an end-to-end run on a spare machine):
# .env.example with random passwords and freshly generated signing and encryption keys.
# Needs uv (https://docs.astral.sh/uv/) for the key generator.
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ -f .env ]; then
  echo "There is already a .env here; not overwriting it." >&2
  exit 1
fi
keys=$(cd backend && uv run --frozen --quiet python -m app.cli generate-secrets)
grep -vE '^(JWT_PRIVATE_KEY_PEM|ENCRYPTION_KEYS|ENCRYPTION_ACTIVE_KEY_VERSION)=' .env.example > .env
for name in POSTGRES_PASSWORD XPLO_OWNER_PASSWORD XPLO_APP_PASSWORD XPLO_ADMIN_PASSWORD REDIS_PASSWORD \
  S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY; do
  sed -i "s/^$name=.*/$name=$(openssl rand -hex 16)/" .env
done
printf '%s\n' "$keys" >> .env
chmod 600 .env
echo "Wrote .env for a test stack."
