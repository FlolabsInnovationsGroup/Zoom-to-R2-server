"""SQLite manifest. Schema = contracts/manifest_schema.sql. Default rollback journal, never WAL."""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import CONTRACTS

COLS = ["zoom_file_id", "meeting_uuid", "meeting_id", "host_email", "topic", "account", "team", "source",
        "recording_type", "file_type", "zoom_size", "r2_key", "meeting_key", "start_time_utc",
        "duration_min", "recording_start", "unmapped"]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Manifest:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.c.executescript((CONTRACTS / "manifest_schema.sql").read_text(encoding="utf-8"))

    @classmethod
    def snapshot(cls, path: str) -> "Manifest":
        """In-memory copy for --dry-run: the file on disk is never touched."""
        m = cls(":memory:")
        if Path(path).is_file():
            src = sqlite3.connect(path)
            src.backup(m.c)
            src.close()
        return m

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> None:
        with self.lock:
            self.c.execute(sql, args)
            self.c.commit()

    def upsert_file(self, row: dict) -> None:
        """New rows start pending. Zoom-side fields refresh on every listing until verified."""
        sets = ", ".join(f"{c} = excluded.{c}" for c in COLS[1:])
        self.x(f"INSERT INTO files ({', '.join(COLS)}, status, attempts, updated_at) "
               f"VALUES ({', '.join('?' * len(COLS))}, 'pending', 0, ?) "
               f"ON CONFLICT(zoom_file_id) DO UPDATE SET {sets} WHERE files.status != 'verified'",
               tuple(row[c] for c in COLS) + (now(),))

    def set_status(self, fid: str, status: str, error=None, r2_size=None, sha256=None, attempt=False) -> None:
        self.x("UPDATE files SET status = ?, last_error = ?, updated_at = ?, "
               "r2_size = COALESCE(?, r2_size), sha256 = COALESCE(?, sha256), attempts = attempts + ? "
               "WHERE zoom_file_id = ?", (status, error, now(), r2_size, sha256, int(attempt), fid))

    def record_trash(self, t: dict, seen: str) -> None:
        self.x("INSERT INTO trash_items (meeting_uuid, meeting_id, host_email, topic, start_time_utc, total_size, "
               "deleted_time, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(meeting_uuid) DO UPDATE SET total_size = excluded.total_size, "
               "deleted_time = COALESCE(excluded.deleted_time, deleted_time), last_seen_at = excluded.last_seen_at",
               (t["uuid"], t.get("id"), t.get("host_email"), t.get("topic"), t.get("start_time"),
                t.get("total_size"), t.get("deleted_time"), seen, seen))

    def serialize(self) -> bytes:
        with self.lock:
            return self.c.serialize()
