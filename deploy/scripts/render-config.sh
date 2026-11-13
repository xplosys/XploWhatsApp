#!/bin/bash
# Writes the files in deploy/secrets/ that services read, from values in .env:
#   metrics_token     the bearer token Prometheus sends to the API (METRICS_TOKEN)
#   alertmanager.yml  where alerts are emailed (ALERT_EMAIL_TO, SMTP_*)
# Run after editing .env: deploy/scripts/render-config.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "No .env here. Copy .env.example to .env first." >&2; exit 1; }
set -a
# shellcheck disable=SC1091  # .env exists only on the server
. ./.env
set +a
umask 077
mkdir -p deploy/secrets backups/status
chmod 755 backups backups/status

: "${METRICS_TOKEN:?set METRICS_TOKEN in .env (openssl rand -hex 32)}"
printf '%s' "$METRICS_TOKEN" > deploy/secrets/metrics_token

: "${ALERT_EMAIL_TO:?set ALERT_EMAIL_TO in .env}"
: "${SMTP_HOST:?set SMTP_HOST in .env}"
case "${ALERT_SMTP_REQUIRE_TLS:-true}" in true|false) ;; *) echo "ALERT_SMTP_REQUIRE_TLS must be true or false" >&2; exit 1 ;; esac
# python3 ships with Ubuntu; values are written as quoted YAML strings, so passwords with
# quotes or colons are safe.
ALERT_EMAIL_FROM="${ALERT_EMAIL_FROM:-${EMAIL_FROM:-alerts@localhost}}" SMTP_SMARTHOST="$SMTP_HOST:${SMTP_PORT:-587}" \
ALERT_SMTP_REQUIRE_TLS="${ALERT_SMTP_REQUIRE_TLS:-true}" SMTP_USERNAME="${SMTP_USERNAME:-}" SMTP_PASSWORD="${SMTP_PASSWORD:-}" \
ALERT_EMAIL_TO="$ALERT_EMAIL_TO" python3 - deploy/monitoring/alertmanager.yml.template deploy/secrets/alertmanager.yml <<'PY'
import json, os, re, sys
names = ["SMTP_SMARTHOST", "SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"]
text = open(sys.argv[1]).read()
for name in names:
    text = text.replace("${%s}" % name, json.dumps(os.environ[name]))
text = text.replace("${ALERT_SMTP_REQUIRE_TLS}", os.environ["ALERT_SMTP_REQUIRE_TLS"])
left = re.findall(r"\$\{[A-Z_]+\}", text)
if left:
    sys.exit("unfilled placeholders: " + ", ".join(left))
open(sys.argv[2], "w").write(text)
PY
# The containers run as their own users and need to read these.
chmod 644 deploy/secrets/metrics_token deploy/secrets/alertmanager.yml
echo "Wrote deploy/secrets/metrics_token and deploy/secrets/alertmanager.yml"
