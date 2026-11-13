#!/bin/sh
# Runs inside the Nginx container after the templates are rendered into /etc/nginx/xplo.
# Enables the HTTP site always, and each HTTPS site only once its certificate exists, so
# Nginx can start (and answer Let's Encrypt) before the first certificate is issued.
set -eu
src=/etc/nginx/xplo
dst=/etc/nginx/conf.d
rm -f "$dst"/*.conf
cp "$src/00-http.conf" "$dst/"
enable_site() {
    site="$1"; domain="$2"
    if [ -z "$domain" ]; then return; fi
    if [ -f "/etc/letsencrypt/live/$domain/fullchain.pem" ]; then
        cp "$src/$site" "$dst/"
        echo "xplo: HTTPS enabled for $domain"
    else
        echo "xplo: no certificate for $domain yet; serving HTTP only (see docs/DEPLOYMENT_UBUNTU.md)"
    fi
}
enable_site 10-app.https.conf "${APP_DOMAIN:-}"
enable_site 20-files.https.conf "${FILES_DOMAIN:-}"
# Pick up renewed certificates (certbot renews in its own container).
( while :; do sleep 6h; nginx -s reload; done ) &
