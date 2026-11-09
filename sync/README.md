# sync: Zoom to Cloudflare R2 archiver

Read-only. Streams Zoom cloud recordings straight into R2 (nothing is written to disk) and records
everything in a SQLite manifest. Config comes from the repo-root `.env` (names in `.env.example`).

```
pip install -e ".[dev]"                          # Python 3.12+, run from sync/

python -m zoom_archive --mode check              # token + scopes, users, R2 reachable; uploads nothing
python -m zoom_archive --mode scan               # list every meeting + Trash inventory, no downloads, no R2 writes
python -m zoom_archive --mode backfill --dry-run # plan only
python -m zoom_archive --mode backfill           # scan first, then download newest first (--from 2020-01-01 --to today)
python -m zoom_archive --mode backfill --user PM@flomad.com --limit 5 --concurrency 2
python -m zoom_archive --mode sync               # last 7 days (--lookback-days N)
python -m zoom_archive --mode reindex            # rebuild the transcript/summary search index from R2
python -m zoom_archive --mode verify             # Zoom vs manifest vs R2 (*-account/ folders); report saved locally

pytest                                           # about 45 seconds
```

Options: `--from`, `--to`, `--user EMAIL`, `--meeting-id ID`, `--limit N` (meetings), `--dry-run`,
`--concurrency 1..4`. Reruns skip verified files.

R2 layout (D15): only `pm-account/`, `john-account/`, `buddy-account/` (`<account>-account/<team>/<yyyy>/<folder>/`
with the recordings and `meeting.json`) and `_system/manifest.sqlite` (backup of the manifest, written at the end of a
run). Nothing else is written to R2. `verify` saves `reports/verification-<date>.md` next to the manifest.

Run control (files next to the manifest, see `runctl.py`): a run writes `sync.alive` every 5 s; a second run exits with
code 3 ("Another run is in progress"). Creating `sync.stop` (the admin page Stop button), Ctrl-C or SIGTERM stops
the run: queued files are cancelled, in-flight downloads aborted and left `pending`, then meeting.json files, the search
index and the manifest backup are written for what completed. Exit code 130. Other exit codes: 0 ok, 1 a file failed,
2 error, 3 another run in progress.

Search: after each complete meeting the TRANSCRIPT (.vtt) and SUMMARY files are indexed into `search.sqlite`
(`SEARCH_DB_PATH`, default next to the manifest). Indexing errors are warnings only.
