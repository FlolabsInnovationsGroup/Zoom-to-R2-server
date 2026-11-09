# Deploy: one Hetzner server runs everything

Zoom sync + admin page + MCP server, all from `sync/` (Python). R2 is only storage.
Caddy gives the web app HTTPS on your own domain. Secrets live only in `/etc/zoom-archive/env`, never in git.

1. **Create the server.** Hetzner Cloud: Ubuntu 24.04, 2 vCPU / 4 GB, EU location, your SSH key.
   Firewall inbound: TCP 22 from your IP only, TCP 80 and 443 from anywhere.
2. **DNS.** One A record: `meetings.<yourdomain>` -> the server IP.
3. **Install packages and Caddy.**
   ```bash
   sudo apt update && sudo apt install -y python3-venv git tmux debian-keyring debian-archive-keyring apt-transport-https curl
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install -y caddy
   ```
4. **Code, user, venv.**
   ```bash
   sudo useradd --system --create-home --home-dir /var/lib/zoom-archive zoomarchive
   sudo mkdir -p /opt/zoom-archive /etc/zoom-archive && sudo chown $USER /opt/zoom-archive
   git clone <private-repo-url> /opt/zoom-archive/app
   cd /opt/zoom-archive/app/sync
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```
5. **Settings.** `sudo nano /etc/zoom-archive/env`, one `NAME=value` per line, no quotes:
   `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, `ZOOM_USER_EMAILS`,
   `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`,
   `AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED`, `AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED`,
   `ADMIN_PASSWORD` and `ADMIN_SECRET_KEY`, both made with `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`,
   `MANIFEST_PATH=/var/lib/zoom-archive/manifest.sqlite`, `KEYS_DB_PATH=/var/lib/zoom-archive/keys.sqlite`,
   `TEAMS_YAML=/opt/zoom-archive/app/sync/teams.yaml`, optionally `SEARCH_DB_PATH`. Names are explained in `.env.example`. No value may contain spaces, quotes, `$`, `;` or `#` (step 6 loads this file with bash).
   ```bash
   sudo chown root:zoomarchive /etc/zoom-archive/env && sudo chmod 640 /etc/zoom-archive/env
   sudo chown -R zoomarchive: /var/lib/zoom-archive
   sudo chmod -R o+rX /opt/zoom-archive
   ```
6. **Check once.** The backfill itself is started later from the admin page (step 7), no tmux needed.
   ```bash
   cd /opt/zoom-archive/app/sync
   sudo -u zoomarchive bash -c 'set -a; . /etc/zoom-archive/env; set +a; .venv/bin/python -m zoom_archive --mode check'
   ```
7. **Web app + HTTPS.** Replace `meetings.example.com` with your domain.
   ```bash
   cd /opt/zoom-archive/app/deploy
   sudo cp zoom-archive-web.service /etc/systemd/system/
   sudo cp Caddyfile /etc/caddy/Caddyfile
   sudo systemctl edit caddy        # add the two lines below, save
   #   [Service]
   #   Environment=DOMAIN=meetings.example.com
   sudo systemctl daemon-reload
   sudo systemctl enable --now zoom-archive-web
   sudo systemctl restart caddy
   ```
   Open `https://<domain>/admin`, sign in with `ADMIN_PASSWORD`. If the service does not start:
   `sudo journalctl -u zoom-archive-web -n 30` (it refuses to start without `ADMIN_PASSWORD`).
   The web service loads the FULL env file (Zoom + R2 + admin settings), because the backfill it starts runs with the
   same environment.
   **Start the backfill:** on `/admin`, "Scan Zoom" lists every recording and downloads nothing (the total is known in
   minutes), "Start backfill" downloads everything (1-2 days), "Stop" stops it. Progress, the last log lines and the
   per-account percentages refresh by themselves; "All recordings" lists every meeting with its status.
   Restarting `zoom-archive-web` stops a running backfill (systemd stops its child process); press Start backfill
   again and it resumes, finished files are skipped.
8. **Daily sync** (03:30 UTC, last 7 days) once the backfill has finished (a run refuses to start while another is going).
   ```bash
   sudo cp zoom-archive-sync.service zoom-archive-sync.timer /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now zoom-archive-sync.timer
   systemctl list-timers zoom-archive-sync.timer
   ```
9. **Connect the AI tools.** On `/admin`, create a key (name, tick groups). The page shows, once:
   - **Claude.ai:** Settings -> Connectors -> Add custom connector -> paste the connector URL `https://<domain>/mcp/<key>`.
   - **ChatGPT:** Settings -> Apps & Connectors -> Create (developer mode) -> paste the same URL.
   - **Claude Code:** `claude mcp add --transport http flolabs-meetings https://<domain>/mcp/<key>`

   The key is part of the URL, so treat the URL like a password. Revoke a key on `/admin` and it stops working at once.
   The server never logs request paths (Caddy has no access log and its error log is switched off in the Caddyfile, uvicorn's is off).

**Updating later:** `cd /opt/zoom-archive/app && git pull && sync/.venv/bin/pip install -e sync && sudo systemctl restart zoom-archive-web`.
**Sign everyone out:** change `ADMIN_PASSWORD` in the env file and restart `zoom-archive-web` (logout only clears one browser).
**Sync progress** is live on `/admin` (read from the local manifest). Full-text search lives in `search.sqlite` next to the manifest (`SEARCH_DB_PATH` to move it); `python -m zoom_archive --mode reindex` rebuilds it from R2.
