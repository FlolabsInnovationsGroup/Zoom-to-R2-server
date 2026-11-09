"""python -m zoom_archive --mode check|backfill|sync|scan|reindex|verify"""
import argparse
import logging
import os
import signal
import sys
import threading
from datetime import date, datetime, timedelta, timezone

from . import archive, runctl
from .config import CONTRACTS, load_config
from .manifest import Manifest
from .naming import load_accounts, load_teams
from .r2 import R2
from .zoom import Zoom, redact


def parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m zoom_archive", description="Read-only Zoom -> R2 archiver.")
    p.add_argument("--mode", required=True, choices=["check", "backfill", "sync", "scan", "reindex", "verify"])
    p.add_argument("--from", dest="frm", type=date.fromisoformat, help="YYYY-MM-DD (default 2020-01-01)")
    p.add_argument("--to", type=date.fromisoformat, help="YYYY-MM-DD (default today)")
    p.add_argument("--user", help="only this Zoom user (email)")
    p.add_argument("--meeting-id", help="only this meeting id")
    p.add_argument("--limit", type=int, help="at most N meetings")
    p.add_argument("--dry-run", action="store_true", help="plan only, download and upload nothing")
    p.add_argument("--concurrency", type=int, choices=range(1, 9), help="parallel files, 1-8 (default SYNC_CONCURRENCY)")
    p.add_argument("--lookback-days", type=int, default=7, help="sync mode only (default 7)")
    return p.parse_args(argv)


def _sigterm(signum, frame):
    raise KeyboardInterrupt  # SIGTERM acts like Ctrl-C


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # topics can be Arabic; never crash on a cp1252 console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    cfg = None
    try:
        cfg = load_config()
        today = datetime.now(timezone.utc).date()
        to = args.to or today
        frm = args.frm or (to - timedelta(days=args.lookback_days) if args.mode == "sync" else date(2020, 1, 1))
        dry = args.dry_run
        controlled = args.mode in ("backfill", "sync", "scan", "reindex")
        if controlled and not dry and not runctl.claim(cfg.manifest_path, {
                "pid": os.getpid(), "mode": args.mode, "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}):
            print("Another run is in progress")
            return 3
        db = Manifest.snapshot(cfg.manifest_path) if dry or args.mode == "check" else Manifest(cfg.manifest_path)
        if args.mode == "sync" and not args.frm:  # also re-list meetings with unfinished files, however old
            old = db.q("SELECT MIN(start_time_utc) AS t FROM files WHERE status != 'verified'")[0]["t"]
            if old:
                frm = min(frm, date.fromisoformat(old[:10]))
        ctx = archive.Ctx(cfg, Zoom(cfg), R2(cfg), db, load_teams(cfg.teams_yaml),
                          load_accounts(CONTRACTS / "access_groups.json"), args.mode, dry)
        if args.mode == "check":
            return archive.check(ctx)
        if args.mode == "verify":
            return archive.verify(ctx, frm, to, args.user)
        old_handler = None
        if threading.current_thread() is threading.main_thread():
            old_handler = signal.signal(signal.SIGTERM, _sigterm)
        try:
            if args.mode == "reindex":
                code = archive.run_reindex(ctx)
            else:
                code = archive.run_archive(ctx, frm, to, args.user, args.meeting_id, args.limit,
                                           args.concurrency or min(max(cfg.concurrency, 1), 8))
        finally:
            if old_handler is not None:
                signal.signal(signal.SIGTERM, old_handler)
        print(archive.summary(ctx))
        return code
    except Exception as e:  # noqa: BLE001 - one clean line, never a token
        if cfg is not None and args.mode in ("backfill", "sync", "scan", "reindex") and not args.dry_run:
            runctl.finished(cfg.manifest_path)  # only removes this process's own run files
        print(f"ERROR: {type(e).__name__}: {redact(e)}", file=sys.stderr)
        return 2
