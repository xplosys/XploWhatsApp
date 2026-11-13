# Deploying XploWhatsApp on Ubuntu

This guide takes one Ubuntu 24.04 server from nothing to a running, monitored, backed-up
XploWhatsApp. After that, every merge to `main` deploys itself.

The whole stack runs from `docker-compose.prod.yml` on that one server:

- Nginx (TLS) and certbot;
- the web app, the API, two workers (one for webhooks, sends and documents, one for AI
  replies) and the scheduler;
- Postgres with pgvector, Redis and ClamAV;
- optionally SeaweedFS for files;
- Prometheus, Alertmanager and Grafana with their exporters.

Nothing is built on the server. CI builds the images and pushes them to GitHub's
container registry (GHCR), and the server pulls them.

What you need before starting:

| Item | Why |
| --- | --- |
| A server: 4 vCPU, 8 GB RAM, 80 GB SSD (minimum 2 vCPU, 4 GB) | ClamAV alone needs about 1.5 GB of RAM |
| A domain whose DNS you control | `app.<domain>`, plus `files.<domain>` with self-hosted storage |
| An SMTP account (SES, Postmark, Mailgun, Zoho, ...) | Account emails and alert emails |
| An S3-compatible bucket at another provider or region | Off-site backups (Backblaze B2, Cloudflare R2, Wasabi, AWS S3) |
| A Sentry project (optional but recommended) | Error reports from the API, the workers and browsers |
| Admin access to the GitHub repository | Deploy secrets and the `production` environment |

The commands below assume the server is reachable as `deploy@server` and the domain is
`xplowhatsapp.com`. Replace both.

## 1. Prepare the server

As root, on a fresh Ubuntu 24.04:

```bash
apt-get update && apt-get -y upgrade
apt-get install -y ca-certificates curl gnupg ufw unattended-upgrades python3

# Docker Engine and the compose plugin, from Docker's own repository.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# A user for deploys. Membership of the docker group is root-equivalent: protect its key.
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
install -d -o deploy -g deploy -m 750 /opt/xplowhatsapp

# Firewall: SSH, HTTP and HTTPS only. Grafana and Prometheus are never exposed.
ufw default deny incoming
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

Docker publishes ports by writing iptables rules directly, which bypasses ufw. That is
why `docker-compose.prod.yml` publishes only Nginx (80 and 443) on all addresses and
Grafana, Prometheus and Alertmanager on `127.0.0.1` only. Keep it that way when you edit the file.

Turn off SSH password logins: set `PasswordAuthentication no` in
`/etc/ssh/sshd_config`, then run `systemctl reload ssh`. Do this once your key works.

## 2. DNS

Point `A` (and `AAAA`, if the server has IPv6) records at the server:

- `app.xplowhatsapp.com`: the app and its API (`/api/`).
- `files.xplowhatsapp.com`: only with self-hosted storage (section 4). It is where
  browsers download knowledge files and exports through presigned links.

Wait until `dig +short app.xplowhatsapp.com` returns the server's address before you ask
for certificates.

## 3. The deploy files and `.env`

As `deploy`, in `/opt/xplowhatsapp`, get the compose file and `deploy/` from the
repository at the commit you will run. CI copies them again on every release.

```bash
cd /opt/xplowhatsapp
git clone --depth 1 https://github.com/xplosys/XploWhatsApp.git /tmp/xplo
cp -r /tmp/xplo/docker-compose.prod.yml /tmp/xplo/deploy /tmp/xplo/.env.example .
rm -rf /tmp/xplo
cp .env.example .env && chmod 600 .env
```

Fill in `.env`. Every value is described in `.env.example`. The ones that differ from
development:

| Setting | Production value |
| --- | --- |
| `ENVIRONMENT` | `production` |
| `FRONTEND_BASE_URL`, `API_BASE_URL` | `https://app.xplowhatsapp.com` (both) |
| `CORS_ALLOWED_ORIGINS` | `https://app.xplowhatsapp.com` |
| `APP_DOMAIN` | `app.xplowhatsapp.com` |
| `COMPOSE_PROFILES` | `monitoring`, or `monitoring,storage` with self-hosted files |
| `POSTGRES_PASSWORD`, `XPLO_*_PASSWORD`, `REDIS_PASSWORD` | `openssl rand -hex 24` each |
| `JWT_PRIVATE_KEY_PEM`, `ENCRYPTION_KEYS` | from `generate-secrets` (below) |
| `SMTP_*`, `SMTP_STARTTLS=true`, `EMAIL_FROM` | your mail provider |
| `METRICS_TOKEN` | `openssl rand -hex 32` |
| `GRAFANA_ADMIN_PASSWORD` | `openssl rand -hex 16` |
| `ALERT_EMAIL_TO` | who is woken up when something breaks |
| `SENTRY_DSN`, `SENTRY_DSN_WEB` | from Sentry (section 9) |
| `BACKUP_S3_*` | the off-site bucket (section 8) |
| Meta, AI, Razorpay, Stripe and seller settings | as in docs/META_INTEGRATION.md, docs/AI_PROVIDERS.md and docs/BILLING.md |

Leave `IMAGE_TAG` empty: `deploy.sh` sets it.

Generate the signing key and the encryption keys with the API image. `<tag>` is the first
12 characters of a `main` commit that CI has built; see the repository's Packages page.

```bash
docker run --rm ghcr.io/xplosys/xplowhatsapp-api:<tag> python -m app.cli generate-secrets
```

Paste its output into `.env`. Store a copy of `ENCRYPTION_KEYS` in your password
manager: without it, the stored WhatsApp and payment credentials cannot be decrypted, even
from a backup.

Then write the files that services read from `.env`:

```bash
deploy/scripts/render-config.sh
```

This writes `deploy/secrets/metrics_token` and `deploy/secrets/alertmanager.yml`. Run it
again whenever you change `METRICS_TOKEN`, `ALERT_*` or `SMTP_*`.

## 4. Object storage

Knowledge files and data exports go to a private S3-compatible bucket. Choose one:

- **A managed bucket (simplest):** set `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL` (the
  same URL), `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` and `S3_BUCKET`.
  Leave `storage` out of `COMPOSE_PROFILES` and `FILES_DOMAIN` empty. The bucket must
  not be public.
- **Self-hosted SeaweedFS:** add `storage` to `COMPOSE_PROFILES`. Then set:
  - `S3_ENDPOINT_URL=http://seaweedfs:8333`;
  - `S3_PUBLIC_ENDPOINT_URL=https://files.xplowhatsapp.com`;
  - `FILES_DOMAIN=files.xplowhatsapp.com`;
  - `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` as long random values.

  Nginx serves the files domain for GET and HEAD only. Set `BACKUP_OBJECTS=true` so the
  files are copied off-site too.

## 5. Backup encryption key

Backups are encrypted to a GPG public key. The private key stays off the server, so a
stolen server or a leaked bucket exposes nothing. Make the key on your own computer, not
on the server:

```bash
gpg --quick-generate-key "XploWhatsApp backups <ops@xplowhatsapp.com>" rsa4096 encr never
gpg --armor --export ops@xplowhatsapp.com > backup-public-key.asc
gpg --armor --export-secret-keys ops@xplowhatsapp.com > backup-private-key.asc
```

- Copy `backup-public-key.asc` to `/opt/xplowhatsapp/deploy/secrets/` on the server.
- Keep `backup-private-key.asc` and its passphrase in two safe places, such as a password
  manager and an offline copy. **Without it, no backup can ever be read.**
- The weekly restore check needs the private key on the server. If you accept that
  trade-off, copy it to `deploy/secrets/backup-private-key.asc` with mode 600, and create
  the key without a passphrase (`--passphrase ''`). Otherwise skip the restore-check timer
  (section 8) and run checks by hand, bringing the key each time.

## 6. Certificates and the first deploy

Log the server in to GHCR once, so the first pull works. Use a GitHub token with only
`read:packages`; later deploys bring their own short-lived token.

```bash
echo <token> | docker login ghcr.io -u <github-user> --password-stdin
```

Start Nginx. Without certificates it serves plain HTTP, which is enough to answer Let's
Encrypt. Then request the certificates through it:

```bash
cd /opt/xplowhatsapp
echo "IMAGE_TAG=<tag>" >> .env        # deploy.sh manages this line from now on
docker compose -f docker-compose.prod.yml up -d nginx
docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot \
  certonly --webroot -w /var/www/acme -d app.xplowhatsapp.com \
  --email ops@xplowhatsapp.com --agree-tos --no-eff-email
# With self-hosted storage, the same again with -d files.xplowhatsapp.com
docker compose -f docker-compose.prod.yml restart nginx
```

`docker compose logs nginx` should now show `HTTPS enabled for app.xplowhatsapp.com`.
The certbot service renews certificates twice a day, and Nginx reloads them every six
hours.

Run the first deploy. It pulls the images, starts the databases, runs the migrations,
starts everything and checks that the API and the web app answer:

```bash
deploy/scripts/deploy.sh <tag>
docker compose -f docker-compose.prod.yml logout ghcr.io 2>/dev/null; docker logout ghcr.io
```

Open `https://app.xplowhatsapp.com`, register the first account, and check:

- `https://app.xplowhatsapp.com/metrics` answers 404 (Prometheus reads it on the internal
  network only);
- `curl -sI https://app.xplowhatsapp.com/login` shows `strict-transport-security` and
  `content-security-policy`.

Then set the Meta webhook URL (docs/META_INTEGRATION.md) and the Razorpay and Stripe
webhook URLs (docs/BILLING.md) to this domain.

## 7. Deploys from GitHub

Every push to `main` runs `.github/workflows/release.yml`:

1. It builds the API and web images.
2. It pushes them to `ghcr.io/xplosys/xplowhatsapp-api` and `-frontend`, tagged with the
   first 12 characters of the commit.
3. It copies `docker-compose.prod.yml` and `deploy/` to the server. `deploy/secrets/`
   is excluded, so files there are never overwritten.
4. It runs `deploy/scripts/deploy.sh <tag>` over SSH, using the job's own short-lived
   registry token.

Set this up once:

1. On the server, as `deploy`, create a key pair for CI only. Then add the public half to
   `~/.ssh/authorized_keys`:
   ```bash
   ssh-keygen -t ed25519 -N '' -f ci_deploy -C github-actions
   cat ci_deploy.pub >> ~/.ssh/authorized_keys
   ```
2. In the repository, open **Settings → Environments** and create `production`. Add
   yourself as a required reviewer if you want to approve each deploy. Then add these to
   the environment:
   - Secret `DEPLOY_SSH_KEY`: the contents of `ci_deploy` (then delete that file from the
     server).
   - Secret `DEPLOY_KNOWN_HOSTS`: the output of `ssh-keyscan -t ed25519 <server>`, run
     from your own computer. Check that the fingerprint matches
     `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server.
   - Variable `DEPLOY_HOST`: the server's address. Deploys are skipped while it is unset.
   - Variables `DEPLOY_USER` and `DEPLOY_DIR`: only if they differ from `deploy` and
     `/opt/xplowhatsapp`.
3. Once the first push has created the packages, open **Packages → xplowhatsapp-api →
   Package settings** and give the repository read access. Do the same for
   `xplowhatsapp-frontend`.

To deploy or roll back to an older build, run the **Release** workflow by hand with that
commit's 12-character tag. On the server, `deploy/scripts/deploy.sh <tag>` does the same.

How a deploy behaves:

- A deploy takes an encrypted database dump before migrating (except the very first).
- If the API or the web app doesn't answer within two minutes, the deploy puts the
  previous release back.
- Migrations must work with the previous release's code for that rollback to be safe.
  Write them that way: add columns, backfill, and drop old ones in a later release.

## 8. Backups

The `ops` profile in `docker-compose.prod.yml` defines two one-off jobs:

- `backup`: an encrypted `pg_dump` into `backups/db/`, kept for `BACKUP_KEEP_DAYS`;
- `backup-offsite`: rclone copies of those dumps (and, with `BACKUP_OBJECTS=true`, the
  file bucket) to `BACKUP_S3_BUCKET`, kept for `BACKUP_OFFSITE_KEEP_DAYS`.

Give the off-site bucket a key that can write but not delete, if your provider supports
it. Turn on object lock or versioning too, so a compromised server cannot erase its own
backups.

Install the timers (as root):

```bash
cp /opt/xplowhatsapp/deploy/systemd/xplo-backup.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xplo-backup.timer                 # nightly, 21:00 UTC
# Only if the private key is on the server (section 5):
cp /opt/xplowhatsapp/deploy/systemd/xplo-backup-verify.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xplo-backup-verify.timer          # Sundays, 22:30 UTC
```

Run one now and check it:

```bash
cd /opt/xplowhatsapp
deploy/scripts/backup.sh nightly
deploy/scripts/backup.sh verify    # restores into a scratch database and checks it
```

`verify` prints the schema version, table count, RLS policy count and number of
businesses it found in the restored copy.

Each job writes a status file to `backups/status/`. Prometheus reads them, and these
alerts follow:

| Alert | Fires when |
| --- | --- |
| `BackupMissing` | No successful dump for 26 hours |
| `BackupNeverRan` | No dump has ever been recorded |
| `OffsiteCopyStale` | No off-site copy for 26 hours |
| `RestoreCheckStale` | No restore check for 8 days |

### Restoring

To put the database back to a backup:

```bash
cd /opt/xplowhatsapp
ls backups/db/                                    # or copy one back from the off-site bucket
deploy/scripts/backup.sh restore backups/db/xplowhatsapp-<stamp>.dump.gpg
```

You must type `RESTORE` to confirm. The restore then:

1. stops Nginx, the web app, the API, the workers and the scheduler;
2. decrypts the dump and replaces the database in one transaction;
3. runs migrations, in case the dump predates the running release;
4. starts everything again.

It needs `deploy/secrets/backup-private-key.asc`. Copy it in for the restore if you keep
it off the server, and remove it afterwards. When the app looks right, take a fresh
backup with `deploy/scripts/backup.sh nightly`.

To rebuild on a new server:

1. Follow sections 1 to 6, using the same `.env`. `ENCRYPTION_KEYS` must match.
2. Copy a dump from the off-site bucket into `backups/db/`.
3. Run the restore command above.

## 9. Monitoring and alerts

**Grafana** listens only on the server's loopback. Open it through an SSH tunnel:

```bash
ssh -L 3001:127.0.0.1:3001 -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 deploy@server
# Grafana:      http://localhost:3001 (admin / GRAFANA_ADMIN_PASSWORD)
# Prometheus:   http://localhost:9090 (alert emails link here)
# Alertmanager: http://localhost:9093 (silence an alert during planned work)
```

Two dashboards are provisioned:

- **XploWhatsApp: app** shows:
  - requests;
  - error rate;
  - latency by route;
  - queue depth;
  - WhatsApp sends;
  - AI replies and provider calls.
- **XploWhatsApp: server** shows:
  - CPU, memory and disk;
  - the last backup;
  - Postgres connections and size;
  - Redis memory.

Both are generated by `deploy/monitoring/grafana/build_dashboards.py`. Edit that script
and rerun it, rather than editing the dashboards in Grafana, where changes are lost on
restart.

**Alerts** are defined in `deploy/monitoring/rules/xplowhatsapp.yml` and emailed to
`ALERT_EMAIL_TO` through your SMTP account. They cover:

- a service down;
- API errors and slowness;
- a queue backlog;
- failing WhatsApp sends;
- failing tasks or AI providers;
- the backups;
- disk, memory, Postgres connections and Redis memory.

To check that delivery works, stop the worker for three minutes:

```bash
docker compose -f docker-compose.prod.yml stop worker
# wait for the ServiceDown email, then
docker compose -f docker-compose.prod.yml start worker
```

**Sentry:** create one project for the API and workers (platform Python), and either the
same project or a second one (platform JavaScript) for the browser.

- Put the DSNs in `SENTRY_DSN` and `SENTRY_DSN_WEB`, then redeploy.
- Browser events go through the app's own `/monitoring` path, so ad blockers don't drop
  them. That path forwards only to the configured project.
- Each event carries the release (the commit tag).
- Request bodies, cookies, authorization headers and user details are never sent.
- Tracing is off until you raise `SENTRY_TRACES_SAMPLE_RATE` (for example to `0.05`).

## 10. Routine operations

| Task | Command (in `/opt/xplowhatsapp`) |
| --- | --- |
| See what is running | `docker compose -f docker-compose.prod.yml ps` |
| Follow the API's logs | `docker compose -f docker-compose.prod.yml logs -f --tail 100 api` |
| Change a setting | edit `.env`, run `deploy/scripts/render-config.sh`, then `docker compose -f docker-compose.prod.yml up -d` |
| Roll back | `deploy/scripts/deploy.sh <previous tag>`, or run the Release workflow with that tag |
| Database shell | `docker compose -f docker-compose.prod.yml exec postgres psql -U postgres xplowhatsapp` |
| Rotate the encryption key | see docs/SECURITY.md, "Key rotation" |
| OS updates | unattended-upgrades applies security updates. Reboot when `/var/run/reboot-required` exists; the stack restarts itself. |

**Capacity.** AI replies run on `worker-ai`. It answers about `AI_WORKER_CONCURRENCY`
(default 12) divided by the model's reply time each second: 12 processes and a 2-second
model give about 6 replies a second. Each process uses about 150 MB of RAM. Measured
figures and the load test are in docs/LAUNCH_CHECKLIST.md section 7.

Logs rotate at 20 MB, keeping five files per container. Prometheus keeps
`PROMETHEUS_RETENTION` (30 days by default) of metrics.

## What this setup does not cover

- **High availability:** everything runs on one server, so a server failure means a
  restore on a new one (section 8). The nightly dump sets the worst-case data loss at
  about one day. For less, add WAL archiving or move to a managed Postgres.
- **Paging:** alerts are email only. Alertmanager can also send to Slack, PagerDuty or a
  webhook; add a receiver in `deploy/monitoring/alertmanager.yml.template`.
