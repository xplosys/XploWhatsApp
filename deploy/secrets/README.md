Files the services read, created on the server and never committed:

- `metrics_token`, `alertmanager.yml`: written by `deploy/scripts/render-config.sh` from `.env`.
- `backup-public-key.asc`: the GPG public key backups are encrypted to.
- `backup-private-key.asc` (optional, mode 600): enables the weekly restore check and restores.
  Keep another copy offline: without it, no backup can be read.

See docs/DEPLOYMENT_UBUNTU.md.
