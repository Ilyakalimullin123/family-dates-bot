# Ubuntu deployment

The bot runs as `familybot` from `/opt/family-dates-bot` using systemd.
PostgreSQL uses a local Unix socket with peer authentication; it is not exposed publicly.
The root-owned token file is `/etc/family-dates-bot/token` (mode 600). systemd passes it through `LoadCredential`.

Commands on the server:

```bash
systemctl status family-dates-bot
systemctl restart family-dates-bot
journalctl -u family-dates-bot -n 50 --no-pager
systemctl list-timers family-dates-backup.timer
```

`family-dates-backup.timer` creates daily custom-format PostgreSQL dumps under
`/var/backups/family-dates-bot` and keeps approximately 14 days.
These are local recovery copies, not off-server backups. Copy dumps to a separate
machine/storage regularly to protect against loss of the server itself.

Never run the Mac polling process alongside the server process with the same token.
Never commit tokens, SSH private keys, or database dumps. `.deploy/` is local-only.

For updates: upload an explicit archive of code (exclude venv, secrets and data),
install dependencies in the existing server venv, run unit tests, then restart the service.
Keep a current database backup before changing the schema.
