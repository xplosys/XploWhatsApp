#!/bin/bash
# Starts the production Nginx config with self-signed certificates and checks it: the
# syntax, HTTP to HTTPS redirects, internal paths kept private, HSTS, and that the API
# receives the client's real address. Used by CI; needs Docker.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
work=$(mktemp -d)
net="xplo-nginx-test-$$"
app=app.example.test
files=files.example.test
# shellcheck disable=SC2317  # called by the EXIT trap
cleanup() {
  [ -n "${KEEP:-}" ] && return; docker rm -f "$net-nginx" "$net-api" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

for d in $app $files; do
  mkdir -p "$work/le/live/$d"
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$d" \
    -keyout "$work/le/live/$d/privkey.pem" -out "$work/le/live/$d/fullchain.pem" 2>/dev/null
done
chmod -R a+rX "$work"

docker network create "$net" >/dev/null
# A stand-in API that echoes the headers it receives.
docker run -d --name "$net-api" --network "$net" --network-alias api --network-alias frontend \
  --entrypoint sh nginx:1.28-alpine -c 'cat > /etc/nginx/conf.d/default.conf <<CONF
server { listen 8000; listen 3000; location / { default_type text/plain; return 200 "real-ip=\$http_x_real_ip xff=\$http_x_forwarded_for\n"; } }
CONF
nginx -g "daemon off;"' >/dev/null

docker run -d --name "$net-nginx" --network "$net" \
  -e APP_DOMAIN=$app -e FILES_DOMAIN=$files -e UPLOAD_LIMIT_MB=22 -e NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/xplo \
  -v "$root/deploy/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$root/deploy/nginx/templates:/etc/nginx/templates:ro" \
  -v "$root/deploy/nginx/40-xplo-sites.sh:/docker-entrypoint.d/40-xplo-sites.sh:ro" \
  -v "$work/le:/etc/letsencrypt:ro" --tmpfs /etc/nginx/xplo \
  nginx:1.28-alpine >/dev/null
sleep 3
docker exec "$net-nginx" nginx -t

fail=0
check() {
  local name="$1" want="$2" got="$3"
  if [[ "$got" == *"$want"* ]]; then echo "ok   $name"; else echo "FAIL $name: wanted '$want', got '$got'"; fail=1; fi
}
req() { docker run --rm --network "$net" curlimages/curl:8.16.0 -sk "$@"; }

ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$net-nginx")
check "health on port 80" "200" "$(req -o /dev/null -w '%{http_code}' http://$net-nginx/nginx-health)"
check "http redirects to https" "301 https://$app/login" \
  "$(req -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $app" http://$net-nginx/login)"
check "metrics hidden" "404" "$(req -o /dev/null -w '%{http_code}' --resolve "$app:443:$ip" https://$app/metrics)"
check "health endpoints hidden" "404" "$(req -o /dev/null -w '%{http_code}' --resolve "$app:443:$ip" https://$app/health/ready)"
check "HSTS on pages" "max-age=63072000" "$(req -D - -o /dev/null --resolve "$app:443:$ip" https://$app/login | grep -i strict-transport)"
check "HSTS on the API" "max-age=63072000" "$(req -D - -o /dev/null --resolve "$app:443:$ip" https://$app/api/v1/auth/me | grep -i strict-transport)"
body=$(req --resolve "$app:443:$ip" -H 'X-Real-IP: 1.2.3.4' -H 'X-Forwarded-For: 5.6.7.8' https://$app/api/v1/leads)
check "spoofed X-Real-IP replaced" "real-ip=172." "$body"
check "spoofed X-Forwarded-For replaced" "xff=172." "$body"
check "files domain is download-only" "403" \
  "$(req -o /dev/null -w '%{http_code}' -X PUT --resolve "$files:443:$ip" https://$files/bucket/key)"
check "unknown host gets no app" "301" "$(req -o /dev/null -w '%{http_code}' -H 'Host: other.example' http://$net-nginx/)"
exit $fail
