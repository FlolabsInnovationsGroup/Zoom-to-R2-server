# Deploying on Vercel (serverless)

The admin page, the MCP server and the Zoom to R2 sync (every 2 hours) run as one Vercel function (`api/index.py`).
No server and no disk: the three small SQLite databases (copy progress, API keys, search index) live in the
R2 bucket under `_system/`, next to the recordings.

| What | Where |
| --- | --- |
| Admin page | `https://<your-domain>/admin` |
| MCP for Claude / ChatGPT | `https://<your-domain>/mcp/<api-key>` |
| Automatic sync | Vercel cron every 2 hours, calls `/api/cron/sync` |
| Sync now | "Check Zoom now" button on the admin page |

Needs **Vercel Pro**: company use is not allowed on Hobby, and the sync needs Pro's longer function time
(up to 800 seconds).

## 1. One-time: put the databases in R2 (done 2026-11-10)

From the laptop that ran the backfill:

```bash
cd sync
MANIFEST_PATH=../.local/manifest.sqlite KEYS_DB_PATH=../.local/keys.sqlite python -m zoom_archive --mode upload-db
```

It never overwrites anything already in R2.

## 2. Create the Vercel project

1. In Vercel: **Add New, then Project**, and import `FlolabsInnovationsGroup/Zoom-to-R2-server`.
2. **Root Directory:** the repository root (leave it empty). Framework preset: **Other**. Vercel finds `api/index.py`,
   `requirements.txt` and `vercel.json` on its own.
3. Add the environment variables below (Settings, then Environment Variables, for **Production**).
4. Deploy. Then open `https://<your-domain>/admin` and log in.

## 3. Environment variables

| Name | Value |
| --- | --- |
| `ZOOM_ACCOUNT_ID` | Zoom Server-to-Server OAuth app |
| `ZOOM_CLIENT_ID` | Zoom Server-to-Server OAuth app |
| `ZOOM_CLIENT_SECRET` | Zoom Server-to-Server OAuth app |
| `ZOOM_USER_EMAILS` | `john@flomadlabs.com,PM@flomad.com,zoom.buddy@flolabsinnovations.com` |
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 token, Object Read & Write, bucket `zoom-archive-test` only |
| `R2_SECRET_ACCESS_KEY` | same token |
| `R2_BUCKET` | `zoom-archive-test` |
| `AWS_REQUEST_CHECKSUM_CALCULATION` | `WHEN_REQUIRED` |
| `AWS_RESPONSE_CHECKSUM_VALIDATION` | `WHEN_REQUIRED` |
| `ADMIN_PASSWORD` | the admin page password |
| `ADMIN_SECRET_KEY` | signs the login cookie (long and random) |
| `CRON_SECRET` | new, long and random (at least 16 characters). Vercel sends it with each cron run |
| `SYNC_CONCURRENCY` | `6` |

Optional:
- `ADMIN_ORIGIN`: your custom domain, e.g. `https://meetings.flolabs.com`. Vercel's own addresses work without it.
- `MIN_TRANSCRIPT_WORDS`: default `300`. Meetings with a shorter transcript are not copied.
- `SYNC_DEADLINE_SECONDS`: default `480`. After this, a run starts no new files and the rest waits for the next run.
- `R2_READ_ACCESS_KEY_ID` / `R2_READ_SECRET_ACCESS_KEY`: a read-only R2 token for the MCP's download links.

`DB_IN_R2=1` is set by `api/index.py` itself. Never put these values in the repository.

## 4. Local backup of the databases

Any time, from the laptop (reads R2 only):

```bash
cd sync
python -m zoom_archive --mode download-db
```

This saves `manifest.sqlite`, `keys.sqlite` and `search.sqlite` to `.local/backup/<date>/`.

## How it works

- **Each function instance** keeps copies of the databases in `/tmp` and checks R2 for a newer version at most
  every 30 seconds (one quick request). The first request on a fresh instance downloads the search index
  (68 MB, about 1-2 seconds inside Vercel).
- **API keys** are saved with a conditional write: only if nobody else changed the key list since it was read.
  Two admins editing at once can't overwrite each other.
- **The MCP access log** is one small file per call under `_system/access-log/`.
- **The sync** takes a lock (`_system/sync-lock.json`), so the cron and the button never run at the same time.
  It lists the last 7 days of Zoom recordings, plus any meeting with unfinished files, and copies what is new.
  Files not started within 8 minutes wait for the next run.
- **Nothing is ever deleted** from R2 or Zoom.

## Things to know

- **The MCP key is part of the URL**, and Vercel's request logs show URLs. Those logs are private to your Vercel
  team, but treat access to them like access to the keys. Clients that can send a header can use
  `https://<your-domain>/mcp` with `Authorization: Bearer <api-key>` instead.
- **Login lockout and Sign out apply per function instance.** The password is long and random, so this is fine,
  but change `ADMIN_PASSWORD` (and redeploy) if it ever leaks.
- **Don't run the laptop copy and Vercel at the same time.** The laptop version doesn't use the R2 sync lock.
  Vercel is now the one that syncs.
