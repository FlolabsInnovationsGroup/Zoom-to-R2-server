"""backfill / sync / scan / reindex / verify / check. Streams Zoom downloads straight into R2, never to disk."""
import json
import re
import logging
import os
import threading
import time
import uuid as uuidlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import runctl, search
from . import zoom as zmod
from .manifest import now
from .naming import account_slug, assign_file_names, format_utc, match_team, meeting_path
from .outputs import group, is_complete, meeting_object
from .r2 import SizeMismatch
from .vtt import parse_vtt
from .zoom import HTTPError, Transient, ZoomError, redact

log = logging.getLogger("zoom_archive")


class Stopped(Exception):
    """Raised between download chunks once a stop was requested (sync.stop, Ctrl-C, SIGTERM)."""


class Ctx:
    def __init__(self, cfg, zoom, r2, db, teams, accounts, mode, dry=False):
        self.cfg, self.zoom, self.r2, self.db, self.teams, self.accounts = cfg, zoom, r2, db, teams, accounts
        self.mode, self.dry = mode, dry
        self.lock = threading.Lock()
        self.n = defaultdict(int)          # done, failed, skipped, bytes, planned, planned_bytes
        self.touched, self.changed, self.partial = set(), set(), set()
        self.trash: set[str] = set()
        self.started, self.finished, self.exit_code = now(), None, None
        self.fallback = False
        self.stop = threading.Event()
        self.deadline: float | None = None  # serverless: start no new file after this time (they stay pending)


# --- users and listing -------------------------------------------------------------------------

def get_users(ctx: Ctx, only: str | None = None) -> list[tuple[str, str]]:
    """(user id, email). Falls back to ZOOM_USER_EMAILS when the app lacks the users scope (4711)."""
    try:
        users = [(u["id"], u.get("email") or u["id"]) for u in ctx.zoom.users()]
    except HTTPError as e:
        if e.code != 4711:
            raise
        users = [(m, m) for m in ctx.cfg.zoom_user_emails]
        ctx.fallback = True
        print("NOTE: GET /users failed (missing scope user:read:list_users:admin, code 4711); "
              f"using ZOOM_USER_EMAILS instead ({len(users)} users).")
        if not users:
            raise ZoomError("ZOOM_USER_EMAILS is empty, so there are no users to archive") from None
    if only:
        users = [u for u in users if u[1].lower() == only.lower()]
        if not users:
            raise ZoomError(f"user {only} not found")
    return users


def months(frm: date, to: date):
    """(start, end) windows of at most one month, newest first."""
    d = to
    while d >= frm:
        first = d.replace(day=1)
        yield max(first, frm), d
        d = first - timedelta(days=1)


def ready_files(m: dict) -> tuple[list[dict], int]:
    """Completed files with a download link, and how many files are not completed yet."""
    files = [f for f in m.get("recording_files") or [] if f.get("file_type")]
    ready = [f for f in files if f.get("status") == "completed" and f.get("download_url")]
    return ready, len(files) - len(ready)


# --- one meeting, one file ---------------------------------------------------------------------

UNMAPPED_ACCOUNT = "john"  # D16: meetings no team rule matches are filed in John's folder (Admin reads them)
SIZE_UNRELIABLE = {"SUMMARY"}  # Zoom's file_size for AI summaries is often wrong; accept what arrives


def min_words() -> int:
    try:
        return max(int(os.environ.get("MIN_TRANSCRIPT_WORDS", "300")), 0)
    except ValueError:
        return 300


def count_words(vtt: str) -> int:
    text = re.sub(r"(?m)^\[\d\d:\d\d:\d\d\] (?:[^:\n]{1,80}: )?", "", parse_vtt(vtt))
    return len(re.findall(r"\w+", text))


def transcript_words(ctx: Ctx, uuid: str, ready: list[dict]) -> int | None:
    """Words in the meeting's transcript(s), counted once and cached in meeting_words. 0 = no transcript.
    None = the transcript could not be read now (the meeting is then copied, never skipped on a guess)."""
    got = ctx.db.q("SELECT words FROM meeting_words WHERE meeting_uuid = ?", (uuid,))
    if got:
        return got[0]["words"]
    words = 0
    for f in ready:
        if (f.get("file_type") or "").upper() == "TRANSCRIPT":
            row = {"meeting_uuid": uuid, "zoom_file_id": str(f["id"])}
            try:
                r = open_download(ctx, row, f)  # refreshes an expired link once
                words += count_words(r.content.decode("utf-8", "replace"))
            except (ZoomError, StopIteration, KeyError) as e:
                log.warning("could not read the transcript of meeting %s to count words: %s", uuid, redact(e))
                return None
    ctx.db.x("INSERT OR REPLACE INTO meeting_words (meeting_uuid, words, counted_at) VALUES (?,?,?)",
             (uuid, words, now()))
    return words


def plan_meeting(ctx: Ctx, m: dict, user_email: str) -> list[tuple[dict, dict]] | None:
    """Upsert the manifest rows of a meeting; return (row, zoom file) to archive, None if nothing is ready."""
    ready, not_ready = ready_files(m)
    if not_ready:
        ctx.partial.add(m["uuid"])
        ctx.n["skipped"] += not_ready
    if not ready:
        return None
    has_transcript = any((f.get("file_type") or "").upper() == "TRANSCRIPT" for f in ready)
    if not_ready and not has_transcript:
        return None  # the transcript may still be processing in Zoom: decide on a later run
    uuid, host = m["uuid"], m.get("host_email") or user_email
    old = ctx.db.q("SELECT * FROM files WHERE meeting_uuid = ?", (uuid,))
    done = {r["zoom_file_id"]: r for r in old if r["status"] == "verified"}
    if done:  # a meeting never moves once something was archived under it
        v = next(iter(done.values()))
        account, team, unmapped = v["account"], v["team"], bool(v["unmapped"])
    else:
        account = account_slug(host, ctx.accounts)
        team, unmapped = match_team(ctx.teams, m.get("topic"), m["id"])
        if unmapped:
            account = UNMAPPED_ACCOUNT
    start = format_utc(m["start_time"])
    path = meeting_path(account, team, m["id"], uuid, m.get("topic"), start)
    if done:  # same folder as the verified files even if the topic changed on Zoom since
        key = v["r2_key"].rsplit("/", 1)[0] + "/"
        path = path._replace(meeting_key=v["meeting_key"], folder=key)
    names = assign_file_names(path, ready,
                              {fid: r["r2_key"].rsplit("/", 1)[-1] for fid, r in done.items()})
    words = None if done or not min_words() else transcript_words(ctx, uuid, ready)
    short = words is not None and words < min_words()
    ctx.touched.add(uuid)
    tasks = []
    for f in ready:
        fid = str(f["id"])
        row = {"zoom_file_id": fid, "meeting_uuid": uuid, "meeting_id": m["id"], "host_email": host,
               "topic": m.get("topic"), "account": account, "team": team,
               "source": "trash" if uuid in ctx.trash else "active", "recording_type": f.get("recording_type"),
               "file_type": f["file_type"], "zoom_size": int(f.get("file_size") or 0),
               "r2_key": path.folder + names[fid], "meeting_key": path.meeting_key, "start_time_utc": start,
               "duration_min": m.get("duration"), "recording_start": f.get("recording_start"),
               "unmapped": int(unmapped)}
        ctx.db.upsert_file(row)
        if fid in done:
            ctx.n["skipped"] += 1
        elif short:
            ctx.db.set_status(fid, "skipped", error=f"transcript has {words} words (under {min_words()})")
        else:
            ctx.db.x("UPDATE files SET status = 'pending', last_error = NULL WHERE zoom_file_id = ? "
                     "AND status = 'skipped'", (fid,))  # the word limit was lowered since
            tasks.append((row, f))
    if short:
        ctx.n["short"] += 1
    return tasks


def open_download(ctx: Ctx, row: dict, f: dict):
    try:
        return ctx.zoom.request(f["download_url"], stream=True)
    except HTTPError as e:
        if e.status not in (403, 404):
            raise
    # The link may have expired: ask Zoom for a fresh one, once.
    fresh = ctx.zoom.meeting_recordings(row["meeting_uuid"])
    url = next(x["download_url"] for x in fresh["recording_files"] if str(x["id"]) == row["zoom_file_id"])
    return ctx.zoom.request(url, stream=True)


def stoppable(ctx: Ctx, chunks):
    for c in chunks:
        if ctx.stop.is_set():
            raise Stopped()
        yield c


def transfer(ctx: Ctx, row: dict, f: dict) -> str:
    for attempt in range(3):
        try:
            chunks = stoppable(ctx, ctx.zoom.chunks(open_download(ctx, row, f)))
            return ctx.r2.upload_stream(row["r2_key"], chunks, row["zoom_size"],
                                        exact=row["file_type"] not in SIZE_UNRELIABLE)
        except Transient:
            if ctx.stop.is_set():
                raise Stopped() from None
            if attempt == 2:
                raise
            time.sleep(zmod.BACKOFF * 2 ** attempt)
    raise AssertionError("unreachable")


def archive_file(ctx: Ctx, row: dict, f: dict) -> None:
    fid, key, size = row["zoom_file_id"], row["r2_key"], row["zoom_size"]
    if ctx.dry:
        with ctx.lock:
            ctx.n["planned"] += 1
            ctx.n["planned_bytes"] += size
        return
    if ctx.stop.is_set() or (ctx.deadline and time.time() > ctx.deadline):
        return  # stays pending for the next run
    ctx.db.set_status(fid, "pending", attempt=True)
    try:
        exact = row["file_type"] not in SIZE_UNRELIABLE
        uploaded = not exact or ctx.r2.head(key) != size
        sha = transfer(ctx, row, f) if uploaded else ctx.r2.sha256(key)
        got = ctx.r2.head(key)
        if (got != size) if exact else not got:
            raise SizeMismatch(f"size mismatch: zoom {size}, R2 has {got}")
        ctx.db.set_status(fid, "verified", r2_size=got, sha256=sha)
        with ctx.lock:
            ctx.n["done"] += 1
            ctx.n["bytes"] += size if uploaded else 0
            ctx.changed.add(row["meeting_uuid"])
    except Stopped:  # aborted in flight: back to pending, not a failure
        ctx.db.set_status(fid, "pending")
    except Exception as e:  # noqa: BLE001 - one bad file must not stop the run
        msg = redact(e if isinstance(e, (SizeMismatch, ZoomError)) else f"{type(e).__name__}: {e}")
        ctx.db.set_status(fid, "failed", error=msg)
        with ctx.lock:
            ctx.n["failed"] += 1
        log.error("file %s failed: %s", fid, msg)


# --- run control -------------------------------------------------------------------------------

def _beat(ctx: Ctx, done: threading.Event) -> None:
    """Daemon thread: sync.alive every runctl.HEARTBEAT seconds; a sync.stop file sets ctx.stop."""
    path, info = ctx.cfg.manifest_path, {"pid": os.getpid(), "mode": ctx.mode, "started_at": ctx.started}
    while True:
        try:
            runctl.heartbeat(path, info)
            if runctl.stop_requested(path):
                ctx.stop.set()
        except OSError as e:
            log.warning("run control: %s", e)
        if done.wait(runctl.HEARTBEAT):
            return


@contextmanager
def run_control(ctx: Ctx):
    if ctx.dry:
        yield
        return
    done = threading.Event()
    t = threading.Thread(target=_beat, args=(ctx, done), daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join(5)
        runctl.finished(ctx.cfg.manifest_path)


def start_run(ctx: Ctx) -> str:
    run_id = uuidlib.uuid4().hex
    ctx.db.x("INSERT INTO runs (run_id, mode, started_at) VALUES (?,?,?)", (run_id, ctx.mode, ctx.started))
    return run_id


def end_run(ctx: Ctx, run_id: str) -> None:
    ctx.exit_code = 130 if ctx.stop.is_set() else (1 if ctx.n["failed"] else 0)
    ctx.finished = now()
    unmapped = sorted({r["topic"] or "" for r in ctx.db.q("SELECT topic FROM files WHERE unmapped = 1")})
    ctx.db.x("UPDATE runs SET finished_at = ?, files_done = ?, files_failed = ?, bytes = ?, files_skipped = ?, "
             "exit_code = ?, unmapped_topics = ? WHERE run_id = ?",
             (ctx.finished, ctx.n["done"], ctx.n["failed"], ctx.n["bytes"], ctx.n["skipped"], ctx.exit_code,
              json.dumps(unmapped, ensure_ascii=False), run_id))


# --- the run -----------------------------------------------------------------------------------

def list_all(ctx: Ctx, frm: date, to: date, users, meeting_id, limit) -> tuple[list, int]:
    """Scan: list every month for every user and upsert all ready files as pending.
    Returns ([(meeting start, manifest row, zoom file)], number of meetings)."""
    seen, tasks, count = set(), [], 0
    for a, b in months(frm, to):
        for uid, email in users:
            if ctx.stop.is_set() or (limit and count >= limit):
                return tasks, count
            for m in ctx.zoom.recordings(uid, a, b):
                if m["uuid"] in seen or (meeting_id and str(m["id"]) != str(meeting_id)):
                    continue
                if limit and count >= limit:
                    break
                planned = plan_meeting(ctx, m, email)
                seen.add(m["uuid"])
                if planned is not None:
                    count += 1
                    tasks += [(row["start_time_utc"], row, f) for row, f in planned]
    return tasks, count


def download(ctx: Ctx, tasks: list, workers: int) -> None:
    """Newest meeting first. On stop or Ctrl-C queued files are cancelled and in-flight ones aborted (pending)."""
    tasks = sorted(tasks, key=lambda t: t[0], reverse=True)  # stable: files of one meeting stay together
    with ThreadPoolExecutor(workers) as pool:
        pending = {pool.submit(archive_file, ctx, row, f) for _, row, f in tasks}
        try:
            while pending and not ctx.stop.is_set():
                _, pending = wait(pending, timeout=0.2)
        except KeyboardInterrupt:
            ctx.stop.set()
        if ctx.stop.is_set():
            log.warning("Stopping: queued files are cancelled, in-flight downloads aborted (left pending)")
            pool.shutdown(wait=True, cancel_futures=True)


def run_archive(ctx: Ctx, frm: date, to: date, user=None, meeting_id=None, limit=None, workers=3) -> int:
    """backfill / sync / scan. Lists everything first, then (not for scan) downloads newest first."""
    run_id = start_run(ctx)
    with run_control(ctx):
        try:
            users = get_users(ctx, user)
            for uid, email in users:  # Trash inventory (Trash items cannot be downloaded)
                try:
                    for t in ctx.zoom.trash(uid):
                        files = t.get("recording_files") or [{}]
                        ctx.db.record_trash({**t, "host_email": t.get("host_email") or email,
                                             "deleted_time": t.get("deleted_time") or files[0].get("deleted_time")},
                                            now())
                except ZoomError as e:
                    log.warning("could not list Trash for %s: %s", email, redact(e))
            ctx.trash = {r["meeting_uuid"] for r in ctx.db.q("SELECT meeting_uuid FROM trash_items")}
            tasks, ctx.n["meetings"] = list_all(ctx, frm, to, users, meeting_id, limit)
            if ctx.mode != "scan":
                download(ctx, tasks, workers)
        except KeyboardInterrupt:
            ctx.stop.set()
        if not ctx.dry:
            finalize(ctx, run_id)
    if ctx.stop.is_set():
        return 130
    return ctx.exit_code if ctx.exit_code is not None else (1 if ctx.n["failed"] else 0)


# --- search index ------------------------------------------------------------------------------

def read_object(r2, key: str) -> bytes:
    return r2.s3.get_object(Bucket=r2.bucket, Key=key)["Body"].read()


def summary_text(doc) -> list[str]:
    if not isinstance(doc, dict):
        return []
    out = [str(doc.get("overall_summary") or "")]
    out += [f"{i.get('label') or ''}: {i.get('summary') or ''}" for i in doc.get("items") or []]
    out.append(str(doc.get("markdown") or ""))
    return [x for x in out if x.strip(": ")]


def index_meeting(ctx: Ctx, m: dict) -> None:
    """Put the transcript and summary text of one complete meeting into the search db. Never raises."""
    try:
        files = sorted(m["files"], key=lambda f: f["name"])
        vtts = [f for f in files if f["file_type"].upper() == "TRANSCRIPT" and f["name"].endswith(".vtt")]
        text = "".join(parse_vtt(read_object(ctx.r2, f["r2_key"]).decode("utf-8", "replace")) for f in vtts)
        if text:
            search.put_meeting(ctx.cfg.search_db_path, m["meeting_key"], "transcript", text)
        parts = []
        for f in (f for f in files if f["file_type"].upper() == "SUMMARY"):
            parts += summary_text(json.loads(read_object(ctx.r2, f["r2_key"])))
        if parts:
            search.put_meeting(ctx.cfg.search_db_path, m["meeting_key"], "summary", "\n".join(parts))
    except Exception as e:  # noqa: BLE001 - indexing must never fail a run
        log.warning("could not index %s: %s", m["meeting_key"], redact(f"{type(e).__name__}: {e}"))


def finalize(ctx: Ctx, run_id: str) -> None:
    """meeting.json, search index, Trash flags, run row, manifest backup. A scan writes nothing to R2."""
    groups = group(ctx.db.q("SELECT * FROM files ORDER BY start_time_utc, recording_start, zoom_file_id"))
    complete = {u: g for u, g in groups.items() if is_complete(g) and u not in ctx.partial}
    meetings = {u: meeting_object(g) for u, g in complete.items()}
    if ctx.mode != "scan":
        for u in ctx.touched & complete.keys():
            m = meetings[u]
            key = m["folder"] + "meeting.json"
            if u in ctx.changed or ctx.r2.head(key) is None:
                ctx.r2.write(key, (json.dumps(m, ensure_ascii=False, indent=2) + "\n").encode())
        for u in ctx.changed & complete.keys():
            index_meeting(ctx, meetings[u])
    for t in ctx.db.q("SELECT meeting_uuid FROM trash_items WHERE archived = 0"):
        if t["meeting_uuid"] in complete:
            ctx.db.x("UPDATE trash_items SET archived = 1 WHERE meeting_uuid = ?", (t["meeting_uuid"],))
    end_run(ctx, run_id)
    if ctx.mode != "scan":
        blob = ctx.db.serialize()
        ctx.r2.write("_system/manifest.sqlite", blob)
        # A dated copy as well: a lost or wrong local manifest must not be able to overwrite the only backup.
        ctx.r2.write(f"_system/manifest-{datetime.now(timezone.utc):%Y-%m-%d}.sqlite", blob)


def run_reindex(ctx: Ctx) -> int:
    """Rebuild the search index of every complete meeting in the manifest from R2."""
    run_id = start_run(ctx)
    groups = [g for g in group(ctx.db.q("SELECT * FROM files ORDER BY start_time_utc, zoom_file_id")).values()
              if is_complete(g)]
    ctx.n["meetings"] = len(groups)
    with run_control(ctx):
        try:
            for g in groups:
                if ctx.stop.is_set():
                    break
                if not ctx.dry:
                    index_meeting(ctx, meeting_object(g))
                    ctx.n["done"] += 1
        except KeyboardInterrupt:
            ctx.stop.set()
        if not ctx.dry:
            end_run(ctx, run_id)
    return 130 if ctx.stop.is_set() else 0


def summary(ctx: Ctx) -> str:
    n = ctx.n
    state = "stopped" if ctx.stop.is_set() else "done"
    if ctx.mode == "reindex":
        return f"reindex {state}: {n['done']} of {n['meetings']} complete meetings indexed."
    if ctx.dry:
        return (f"DRY RUN: {n['meetings']} meetings, {n['planned']} files ({n['planned_bytes']:,} bytes) would be "
                f"downloaded, {n['skipped']} skipped (already verified or still processing). Nothing was written.")
    if ctx.mode == "scan":
        total = ctx.db.q("SELECT COUNT(*) AS n, COALESCE(SUM(zoom_size), 0) AS b FROM files")[0]
        todo = ctx.db.q("SELECT COUNT(*) AS n, COALESCE(SUM(zoom_size), 0) AS b FROM files "
                        "WHERE status != 'verified'")[0]
        return (f"scan {state}: {n['meetings']} meetings listed, {total['n']} files ({total['b']:,} bytes) in the "
                f"manifest, {todo['n']} files ({todo['b']:,} bytes) still to download.")
    rows = ctx.db.q("SELECT topic FROM files WHERE unmapped = 1 GROUP BY topic")
    out = [f"{ctx.mode} {state}: {n['meetings']} meetings, {n['done']} files verified "
           f"({n['bytes']:,} bytes uploaded), {n['skipped']} skipped, {n['failed']} failed."]
    if rows:
        out.append("Unmapped topics (went to default team): " + "; ".join(r["topic"] or "" for r in rows))
    out += [f"FAILED {r['zoom_file_id']} ({r['meeting_key']}): {r['last_error']}"
            for r in ctx.db.q("SELECT * FROM files WHERE status = 'failed'")[:20]]
    return "\n".join(out)


# --- check and verify --------------------------------------------------------------------------

def check(ctx: Ctx) -> int:
    ctx.zoom.token()
    print(f"Zoom token OK, {len(ctx.zoom.scopes)} scopes, all read-only: {', '.join(ctx.zoom.scopes)}")
    users = get_users(ctx)
    print(f"Zoom users: {len(users)} ({', '.join(e for _, e in users)})")
    print(f"R2 bucket {ctx.r2.bucket} reachable, listed {len(ctx.r2.list(max_keys=5))} keys (max 5). Nothing uploaded.")
    return 0


def verify(ctx: Ctx, frm: date, to: date, user=None) -> int:
    """Fresh Zoom listing vs manifest vs R2 objects under every <account>-account/ prefix. The report is
    printed and saved to <manifest dir>/reports/verification-<date>.md (nothing is written to R2)."""
    zoom_files: dict[str, int] = {}
    for uid, _ in get_users(ctx, user):
        for a, b in months(frm, to):
            for m in ctx.zoom.recordings(uid, a, b):
                for f in ready_files(m)[0]:
                    zoom_files[str(f["id"])] = int(f.get("file_size") or 0)
    rows = {r["zoom_file_id"]: r for r in ctx.db.q("SELECT * FROM files")}
    verified = {k: r for k, r in rows.items() if r["status"] == "verified"}
    accounts = set(ctx.accounts.values()) | {r["account"] for r in rows.values()}
    objs = {k: s for a in sorted(accounts) for k, s in ctx.r2.list(f"{a}-account/").items()
            if not k.endswith("/meeting.json")}
    keys = {r["r2_key"] for r in verified.values()}
    problems = {
        "In Zoom but not verified in the manifest": [k for k in zoom_files if k not in verified
                                                     and rows.get(k, {}).get("status") != "skipped"],
        "Verified in the manifest but missing or wrong size in R2":
            [r["r2_key"] for r in verified.values() if objs.get(r["r2_key"]) != (r["r2_size"] or r["zoom_size"])],
        "Verified size differs from Zoom": [k for k, r in verified.items() if k in zoom_files
                                            and r["file_type"] not in SIZE_UNRELIABLE
                                            and zoom_files[k] != r["zoom_size"]],
        "In R2 but not in the manifest": [k for k in objs if k not in keys],
    }
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [f"# Verification {today}", "",
             f"- Zoom completed files: {len(zoom_files)} ({sum(zoom_files.values()):,} bytes)",
             f"- Manifest verified files: {len(verified)} ({sum(r['zoom_size'] for r in verified.values()):,} bytes)",
             f"- R2 recording objects under the *-account/ folders: {len(objs)} ({sum(objs.values()):,} bytes)", ""]
    for title, items in problems.items():
        lines.append(f"## {title}: {len(items)}")
        lines += [f"- {i}" for i in items[:50]]
        lines.append("")
    report = "\n".join(lines)
    out = Path(ctx.cfg.manifest_path).parent / "reports" / f"verification-{today}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report saved to {out}")
    return 1 if any(problems.values()) else 0
