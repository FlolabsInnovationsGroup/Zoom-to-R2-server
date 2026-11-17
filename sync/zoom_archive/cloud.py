"""Serverless sync (Vercel): one time-limited sync run inside a function, from the cron (every 2 hours) or the admin button.

Same work as `--mode sync` on a server: list the last LOOKBACK_DAYS of Zoom recordings (plus any meeting with
unfinished files), copy what is new to R2, update meeting.json and the search index. Files not started before the
deadline stay pending for the next run, so a big day never overruns the function's time limit.
"""
import logging
import os
import threading
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
SAVE_EVERY = 60     # seconds between saves of the copy progress file to R2 during a run
PROGRESS_EVERY = 5  # seconds between live-progress updates (and lock renewals) for the admin page


def progress(ctx) -> dict:
    """What the admin page shows while a run works."""
    with ctx.lock:
        live = [dict(x) for x in ctx.live.values()]
        n = dict(ctx.n)
    return {"phase": ctx.phase, "files_total": n.get("queued", 0),
            "files_done": n.get("done", 0) + n.get("failed", 0) + n.get("gone", 0),
            "failed": n.get("failed", 0), "bytes_total": n.get("queued_bytes", 0),
            "bytes_done": n.get("done_bytes", 0) + sum(x["done"] for x in live),
            "copy_started": ctx.copy_started, "at": time.time(),
            "current": [{"topic": x["topic"][:80], "type": x["type"],
                         "pct": round(100 * x["done"] / x["size"]) if x["size"] else 0} for x in live][:8]}


def deadline_seconds() -> int:
    """Stop starting new files after this many seconds (default 480, leaving room for a 2 GB file to finish
    within Vercel Pro's 800 s limit)."""
    try:
        return max(30, int(os.environ.get("SYNC_DEADLINE_SECONDS") or 480))
    except ValueError:
        return 480


def run_sync(who: str) -> dict:
    """One sync run. Returns a short summary dict (also kept in the sync lock object for the admin page)."""
    try:
        cfg = load_config()
        r2 = R2(cfg)
        token = r2db.take_lock(r2, who)
    except Exception as e:  # noqa: BLE001 - settings or R2 unreachable: say so instead of a bare 500
        msg = redact(f"{type(e).__name__}: {e}")
        log.error("sync could not start: %s", msg)
        return {"ok": False, "error": msg}
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
        done = threading.Event()

        def keep_saving():
            """Live progress every few seconds; copied files reach R2's progress file within a minute, so a
            cut-off run loses at most that."""
            last_save = time.time()
            while not done.wait(PROGRESS_EVERY):
                if not r2db.renew_lock(r2, token, progress(ctx)):
                    log.warning("sync lock taken over by another run")
                if time.time() - last_save >= SAVE_EVERY:
                    last_save = time.time()
                    try:
                        r2.write(r2db.KEYS["manifest"], db.serialize())
                    except Exception as e:  # noqa: BLE001 - try again next minute
                        log.warning("progress save failed: %s", redact(e))

        saver = threading.Thread(target=keep_saving, daemon=True)
        saver.start()
        try:
            code = archive.run_archive(ctx, frm, to, workers=max(1, min(cfg.concurrency, 8)))
        finally:
            done.set()
            saver.join(30)
            r2.write(r2db.KEYS["manifest"], db.serialize())  # the final state, after any periodic save
        left = db.q("SELECT COUNT(*) AS n FROM files WHERE status = 'pending'")[0]["n"]
        db.c.close()
        r2db.forget("manifest")  # re-read it from R2 next time
        if ctx.changed and os.path.isfile(cfg.search_db_path):
            r2db.push(r2, "search")
        summary.update(ok=code in (0, 1), files=ctx.n["done"], failed=ctx.n["failed"], skipped=ctx.n["skipped"],
                       gone=ctx.n["gone"], bytes=ctx.n["bytes"], meetings=ctx.n["meetings"], left=left)
    except Exception as e:  # noqa: BLE001 - reported on the admin page, never a token
        log.error("sync failed: %s", redact(f"{type(e).__name__}: {e}"))
        summary.update(ok=False, error=redact(f"{type(e).__name__}: {e}"))
    summary["finished"] = now()
    r2db.release_lock(r2, token, summary)
    return summary
