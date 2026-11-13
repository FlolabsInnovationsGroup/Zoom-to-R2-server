"""Serverless mode (Vercel): the SQLite files live in R2 instead of on a server disk.

Turned on with DB_IN_R2=1. Each function instance keeps a local copy in /tmp and re-checks R2 at most every
CHECK_EVERY seconds (one HEAD call), downloading only when the file changed.

  _system/manifest.sqlite   copy progress. Written only by the sync (it already wrote this backup before).
  _system/search.sqlite     search index. Written only by the sync.
  _system/keys.sqlite       API keys. Written by the admin page, always with a conditional write (only if nobody
                            changed it since it was read), so two edits can never overwrite each other.
  _system/access-log/...    one small object per MCP call (newest first by name), so calls never clash.
  _system/sync-lock.json    who is syncing; stops two syncs (cron + button) from running at once.

Nothing here deletes anything in R2.
"""
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .r2 import Conflict

KEYS = {"manifest": "_system/manifest.sqlite", "search": "_system/search.sqlite", "keys": "_system/keys.sqlite"}
LOG_PREFIX = "_system/access-log/"
LOCK_KEY = "_system/sync-lock.json"
CHECK_EVERY = 30      # seconds between "did it change in R2?" checks per file
LOCK_STALE = 20 * 60  # a sync lock older than this is from a run that died (functions stop after at most 800 s)

_state: dict[str, dict] = {}  # name -> {"etag": str | None, "checked": float}
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("DB_IN_R2", "").strip() == "1"


def path(name: str) -> str:
    d = Path(os.environ.get("ZOOM_ARCHIVE_TMP") or tempfile.gettempdir()) / "zoom-archive"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{name}.sqlite")


def forget(name: str) -> None:
    """Drop the local copy, so the next pull downloads it again."""
    with _lock:
        _state.pop(name, None)
        Path(path(name)).unlink(missing_ok=True)


def pull(r2, name: str, fresh: bool = False) -> str | None:
    """Local path of an up-to-date copy of the file, or None if R2 does not have it yet."""
    p = Path(path(name))
    with _lock:
        st = _state.get(name)
        if st and not fresh and time.time() - st["checked"] < CHECK_EVERY:
            return str(p) if st["etag"] and p.is_file() else None
        etag = r2.etag(KEYS[name])
        if etag and (not st or st["etag"] != etag or not p.is_file()):
            part = p.with_suffix(f".{uuid.uuid4().hex[:8]}.part")
            etag = r2.download(KEYS[name], part)
            os.replace(part, p)  # open connections keep reading the old file; new ones see the new one
        _state[name] = {"etag": etag, "checked": time.time()}
        return str(p) if etag else None


def edit(r2, name: str, change, create=None):
    """Apply change(local_path) to the freshest copy and save it back to R2 only if nobody else saved in between;
    on a clash, start again from their version (up to 3 times). create(local_path) makes a new empty file when
    R2 has none. Returns what change() returned."""
    for _ in range(3):
        p = pull(r2, name, fresh=True)
        etag = _state[name]["etag"]
        try:
            if p is None:
                p = path(name)
                Path(p).unlink(missing_ok=True)
                if create:
                    create(p)
            out = change(p)
            new = r2.write_if(KEYS[name], Path(p).read_bytes(), etag)
            with _lock:
                _state[name] = {"etag": new, "checked": time.time()}
            return out
        except Conflict:
            forget(name)
        except Exception:
            forget(name)  # never keep a half-edited local copy
            raise
    raise Conflict(KEYS[name])


def push(r2, name: str) -> None:
    """Upload the local copy as is (the sync, which holds the sync lock, is the only writer of manifest/search)."""
    data = Path(path(name)).read_bytes()
    r2.write(KEYS[name], data)
    with _lock:
        _state.pop(name, None)  # next pull re-checks R2


# ---- access log: one object per call -------------------------------------------------------------

def log_access(r2, key_id: str, tool: str, meeting_key: str | None, result: str) -> None:
    now = time.time()
    inv = f"{9_999_999_999_999 - int(now * 1000):013d}"  # newest first in R2's key order
    entry = {"at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "key_id": key_id,
             "tool": tool[:64], "meeting_key": meeting_key[:300] if meeting_key else None, "result": result}
    r2.write(f"{LOG_PREFIX}{inv}-{key_id}-{uuid.uuid4().hex[:8]}.json", json.dumps(entry).encode())
    _log_cache["at"] = 0.0  # this instance shows the call at once


_log_cache: dict = {"at": 0.0, "rows": [], "last_used": {}}


def recent_access(r2, limit: int = 50) -> tuple[list[dict], dict[str, str]]:
    """(last `limit` calls, newest first; {key_id: last used}) from the access log. Cached CHECK_EVERY seconds."""
    if time.time() - _log_cache["at"] < CHECK_EVERY:
        return _log_cache["rows"], _log_cache["last_used"]
    names = r2.keys_page(LOG_PREFIX, 1000)
    last_used: dict[str, str] = {}
    for n in names:  # newest first, so the first one seen per key is its last use
        stem = n[len(LOG_PREFIX):]
        inv, _, rest = stem.partition("-")
        key_id = rest[:-14] if len(rest) > 14 else rest  # strip "-xxxxxxxx.json"
        if key_id not in last_used and inv.isdigit():
            ms = 9_999_999_999_999 - int(inv)
            last_used[key_id] = datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for n in names[:limit]:
        try:
            rows.append(json.loads(r2.read(n)))
        except Exception:  # noqa: BLE001 - one unreadable entry must not break the page
            continue
    _log_cache.update(at=time.time(), rows=rows, last_used=last_used)
    return rows, last_used


# ---- sync lock (no delete: released by overwriting) -----------------------------------------------

def take_lock(r2, who: str) -> str | None:
    """Returns a token if this run may sync now, None if another sync holds a fresh lock."""
    from botocore.exceptions import ClientError
    try:
        r = r2.s3.get_object(Bucket=r2.bucket, Key=LOCK_KEY)
        etag, cur = r["ETag"], json.loads(r["Body"].read() or b"{}")
    except ClientError as e:
        if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
        etag, cur = None, {}
    if cur.get("running") and time.time() - float(cur.get("since", 0)) < LOCK_STALE:
        return None
    token = uuid.uuid4().hex
    try:
        r2.write_if(LOCK_KEY, json.dumps({"running": True, "since": time.time(), "by": who, "token": token}).encode(), etag)
    except Conflict:
        return None
    return token


def renew_lock(r2, token: str) -> bool:
    """Called every minute by a running sync, so a long run's lock never looks abandoned. False = not ours any more."""
    try:
        r = r2.s3.get_object(Bucket=r2.bucket, Key=LOCK_KEY)
        cur = json.loads(r["Body"].read() or b"{}")
        if cur.get("token") != token:
            return False
        r2.write_if(LOCK_KEY, json.dumps({**cur, "since": time.time()}).encode(), r["ETag"])
        return True
    except Exception:  # noqa: BLE001 - try again next minute
        return True


def release_lock(r2, token: str, summary: dict) -> None:
    try:
        r = r2.s3.get_object(Bucket=r2.bucket, Key=LOCK_KEY)
        if json.loads(r["Body"].read() or b"{}").get("token") != token:
            return  # someone else's lock (ours went stale and was taken over)
        r2.write_if(LOCK_KEY, json.dumps({"running": False, "last": summary}).encode(), r["ETag"])
    except Exception:  # noqa: BLE001 - a stale lock frees itself after LOCK_STALE
        pass


def lock_state(r2) -> dict:
    try:
        return json.loads(r2.read(LOCK_KEY) or b"{}")
    except Exception:  # noqa: BLE001
        return {}
