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

## Optional calendar subscriptions

The `.ics` download works without a domain. Live subscriptions additionally require a
stable public HTTPS origin reachable by Apple and Google without a login challenge.
No public listener is started unless `CALENDAR_PUBLIC_BASE_URL` is configured.

1. Point a chosen domain/subdomain to the server and configure a TLS reverse proxy.
2. Forward `/calendar/*` and `/robots.txt` to `http://127.0.0.1:8088`. Disable request URL
   logging for calendar routes; their paths contain bearer tokens. Do not use a public
   CDN cache or authentication/cookie challenge for these routes. Keep port 8088 private.
3. Copy the values from `calendar.env.example` into `/etc/family-dates-bot/calendar.env`,
   replacing the example origin with the actual one. The service reads this optional file.
   `CALENDAR_PUBLIC_BASE_URL` must be an HTTPS origin, with no path, query or credentials.
4. Include `calendar_sync.py` and `growth.py` in the code archive. Install requirements,
   run unit tests and the PostgreSQL checks against a test database, back up the production
   database, update the service definition, run `systemctl daemon-reload`, then restart.
5. In a test chat, run `/calendar`, create a subscription and verify it from an external
   network. Check an addition, deletion and link revocation; then test on an iPhone and in
   Google Calendar. Their polling intervals are controlled by those clients.

The web server lives in the bot process; it is not a second polling instance. It binds
only to `127.0.0.1` and defaults to port 8088 (`CALENDAR_HTTP_PORT` changes that port).
The subscription is read-only and uses the same event IDs across refreshes. Revocation
blocks future requests but cannot erase copies already held by calendar providers.
The new `calendar_feeds` table is included in ordinary full-database backups.
