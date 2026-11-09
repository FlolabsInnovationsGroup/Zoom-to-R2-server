"""Who may read what, API keys and the keys database (plan D3 + D11).

Groups come from contracts/access_groups.json. The ONLY readable R2 objects are <account>-account/<team>/...
that one of the key's groups allows (D15). _system/ and everything else is never readable. R2 is reached only
through ReadOnlyR2 (get / head / list / presigned GET link, nothing else).
"""
import base64
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import CONTRACTS
from .r2 import R2

ACCESS = json.loads((CONTRACTS / "access_groups.json").read_text(encoding="utf-8"))
GROUPS: dict = ACCESS["groups"]
MEETING_KEY_RE = re.compile(json.loads((CONTRACTS / "meeting.schema.json").read_text(encoding="utf-8"))
                            ["properties"]["meeting_key"]["pattern"])
API_KEY_RE = re.compile(r"^fma_[A-Za-z0-9_-]{43}$")


def parse_groups(raw) -> list[str]:
    """The access_groups column (JSON array) -> known group ids. Empty = the key cannot read anything."""
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [g for g in v if isinstance(g, str) and g in GROUPS] if isinstance(v, list) else []


def can_read(groups: list[str], account: str, team: str) -> bool:
    """Any group allows (account, team): account "*" = any account, `teams` = only these, `teams_except` = all but
    these, neither = whole account."""
    for g in groups:
        for r in GROUPS.get(g, {}).get("allow", []):
            if r["account"] not in ("*", account):
                continue
            if "teams" in r:
                ok = team in r["teams"]
            elif "teams_except" in r:
                ok = team not in r["teams_except"]
            else:
                ok = True
            if ok:
                return True
    return False


def readable(groups: list[str], key: str) -> bool:
    parts = key.split("/")
    return (len(parts) > 3 and parts[0].endswith("-account") and len(parts[0]) > 8 and ".." not in key
            and can_read(groups, parts[0][:-8], parts[1]))


class ReadOnlyR2:
    """get / head / list / presign only. Keys failing `allow` look missing. for_groups() gives the per-key view."""

    def __init__(self, settings, allow=None):
        self._r2 = R2(settings)
        self._allow = allow or (lambda key: False)

    def for_groups(self, groups: list[str]) -> "ReadOnlyR2":
        view = object.__new__(ReadOnlyR2)
        view._r2, view._allow = self._r2, lambda k: readable(groups, k)
        return view

    def get(self, key: str) -> bytes | None:
        if not self._allow(key):
            return None
        try:
            return self._r2.s3.get_object(Bucket=self._r2.bucket, Key=key)["Body"].read()
        except self._r2.s3.exceptions.NoSuchKey:
            return None

    def head(self, key: str) -> bool:
        return self._allow(key) and self._r2.head(key) is not None

    def presign(self, key: str, seconds: int = 3600) -> str | None:
        """Presigned GET link for an allowed key, else None."""
        if not self._allow(key):
            return None
        return self._r2.s3.generate_presigned_url("get_object", Params={"Bucket": self._r2.bucket, "Key": key},
                                                  ExpiresIn=seconds)

    def list(self, prefix: str) -> list[str]:
        return [k for k in self._r2.list(prefix) if self._allow(k)] if self._allow(prefix + "x") else []


def read_manifest(path: str, *sqls: str) -> list[list[dict]] | None:
    """Run read-only queries on the local manifest (never writes, never blocks the sync). None = no manifest yet."""
    f = Path(path)
    if not f.is_file():
        return None
    try:
        c = sqlite3.connect(f"{f.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            return [[dict(r) for r in c.execute(q)] for q in sqls]
        finally:
            c.close()
    except sqlite3.Error:
        return None


# ---- API keys ("fma_" + 43 base64url chars; only sha256(key) is stored) --------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "fma_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def key_state(row) -> str:
    if row["revoked_at"]:
        return "revoked"
    if row["expires_at"]:  # an unparseable expiry counts as expired
        try:
            exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        except ValueError:
            return "expired"
        if not exp > datetime.now(timezone.utc):
            return "expired"
    return "active"


def connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_keys_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = connect(path)
    c.executescript((CONTRACTS / "d1_schema.sql").read_text(encoding="utf-8"))
    c.close()


def active_key(db_path: str, api_key: str):
    """(key id, groups) if this API key is known, active and holds a known group, else None. Called on EVERY request."""
    if not API_KEY_RE.match(api_key or ""):
        return None
    c = connect(db_path)
    try:
        row = c.execute("SELECT * FROM api_keys WHERE key_hash = ?", (sha256_hex(api_key),)).fetchone()
    finally:
        c.close()
    if not row or key_state(row) != "active":
        return None
    groups = parse_groups(row["access_groups"])
    return (row["id"], groups) if groups else None


def record_access(db_path: str, key_id: str, tool: str, meeting_key: str | None, result: str) -> None:
    """One access_log row plus last_used_at. Never raises: logging must not break a call."""
    at = now_iso()
    try:
        c = connect(db_path)
        with c:
            c.execute("INSERT INTO access_log (key_id, at, tool, meeting_key, result) VALUES (?,?,?,?,?)",
                      (key_id, at, tool[:64], meeting_key[:300] if meeting_key else None, result))
            c.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (at, key_id))
        c.close()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).error("access_log write failed")
