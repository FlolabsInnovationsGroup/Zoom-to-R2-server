"""Serverless sync (Vercel): one time-limited sync run inside a function, from the daily cron or the admin button.

Same work as `--mode sync` on a server: list the last LOOKBACK_DAYS of Zoom recordings (plus any meeting with
unfinished files), copy what is new to R2, update meeting.json and the search index. Files not started before the
deadline stay pending for the next run, so a big day never overruns the function's time limit.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

from . import archive, r2db
from .config import CONTRACTS, load_config
from .manifest import Manifest, now
from .naming import load_accounts, load_teams
from .r2 import R2
from .zoom import Zoom, redact

log = logging.getLogger("zoom_archive")
LOOKBACK_DAYS = 7


def deadline_seconds() -> int:
    """Stop starting new files after this many seconds (default 480, leaving room for a 2 GB file to finish
    within Vercel Pro's 800 s limit)."""
    try:
        return max(30, int(os.environ.get("SYNC_DEADLINE_SECONDS") or 480))
    except ValueError:
        return 480


def run_sync(who: str) -> dict:
    """One sync run. Returns a short summary dict (also kept in the sync lock object for the admin page)."""
    cfg = load_config()
    r2 = R2(cfg)
    token = r2db.take_lock(r2, who)
    if not token:
        return {"ok": False, "busy": True, "message": "Another sync is already running."}
    summary = {"by": who, "started": now()}
    try:
        r2db.pull(r2, "manifest", fresh=True)
        r2db.pull(r2, "search", fresh=True)
        db = Manifest(cfg.manifest_path)
        to = datetime.now(timezone.utc).date()
        frm = to - timedelta(days=LOOKBACK_DAYS)
        old = db.q("SELECT MIN(start_time_utc) AS t FROM files WHERE status NOT IN ('verified', 'skipped')")[0]["t"]
        if old:  # also re-list meetings with unfinished files, however old
            frm = min(frm, date.fromisoformat(old[:10]))
        ctx = archive.Ctx(cfg, Zoom(cfg), r2, db, load_teams(cfg.teams_yaml),
                          load_accounts(CONTRACTS / "access_groups.json"), "sync")
        ctx.deadline = time.time() + deadline_seconds()
        code = archive.run_archive(ctx, frm, to, workers=max(1, min(cfg.concurrency, 8)))
        left = db.q("SELECT COUNT(*) AS n FROM files WHERE status = 'pending'")[0]["n"]
        db.c.close()
        r2db.forget("manifest")  # run_archive already saved it to R2 (finalize); re-read it next time
        if ctx.changed and os.path.isfile(cfg.search_db_path):
            r2db.push(r2, "search")
        summary.update(ok=code in (0, 1), files=ctx.n["done"], failed=ctx.n["failed"], skipped=ctx.n["skipped"],
                       bytes=ctx.n["bytes"], meetings=ctx.n["meetings"], left=left)
    except Exception as e:  # noqa: BLE001 - reported on the admin page, never a token
        log.error("sync failed: %s", redact(f"{type(e).__name__}: {e}"))
        summary.update(ok=False, error=redact(f"{type(e).__name__}: {e}"))
    summary["finished"] = now()
    r2db.release_lock(r2, token, summary)
    return summary
