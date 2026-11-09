"""R2 key layout (D15) and teams.yaml / access_groups.json loading.

<account>-account/<team>/<yyyy>/<YYYY-MM-DD>_<HHMM>utc_<topic-slug>_<uuid8>/<slug>_<DD-MM-YYYY>_<kind>.<ext>
meeting_key = <account>/<team>/<yyyy>/<folder>; r2_folder(meeting_key) is the meeting's R2 folder.
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

import yaml

MEETING_KEY_RE = re.compile(
    r"^[a-z0-9-]+/[a-z0-9-]+/\d{4}/\d{4}-\d{2}-\d{2}_\d{4}utc_[a-z0-9-]+_[0-9a-f]{8}$")
KNOWN_KINDS = {"M4A": "audio", "TRANSCRIPT": "transcript", "CC": "captions", "CHAT": "chat",
               "TIMELINE": "timeline"}
DEFAULT_EXT = {"MP4": "mp4", "M4A": "m4a", "TRANSCRIPT": "vtt", "CC": "vtt", "CHAT": "txt",
               "TIMELINE": "json", "SUMMARY": "json"}


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def format_utc(value: str) -> str:
    return parse_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def cairo_iso(value: str) -> str:
    return parse_utc(value).astimezone(ZoneInfo("Africa/Cairo")).isoformat(timespec="seconds")


def slugify(text: str, keep: str = "") -> str:
    return re.sub(rf"[^a-z0-9{keep}]+", "-", text.lower()).strip("-")


def account_slug(email: str | None, accounts: dict) -> str:
    """Mapped host -> its account. Any other host -> unmapped-<sha8 of the email>, never a name that could equal
    a mapped account (pm@other.com must not land in "pm")."""
    e = (email or "").strip().lower()
    return accounts.get(e) or (f"unmapped-{hashlib.sha256(e.encode()).hexdigest()[:8]}" if e else "unknown")


def topic_slug(topic: str | None, meeting_id) -> str:
    text = unicodedata.normalize("NFKD", topic or "").encode("ascii", "ignore").decode()
    return slugify(text)[:60].strip("-") or f"meeting-{meeting_id}"


def r2_folder(meeting_key: str) -> str:
    """D15: 'pm/design/2026/...' -> 'pm-account/design/2026/.../' (the meeting's R2 folder)."""
    account, rest = meeting_key.split("/", 1)
    return f"{account}-account/{rest}/"


class MeetingPath(NamedTuple):
    meeting_key: str
    folder: str
    slug: str
    date: str  # DD-MM-YYYY


def meeting_path(account, team, meeting_id, uuid, topic, start_utc) -> MeetingPath:
    dt = parse_utc(start_utc)
    slug = topic_slug(topic, meeting_id)
    key = (f"{account}/{team}/{dt:%Y}/{dt:%Y-%m-%d}_{dt:%H%M}utc_{slug}_"
           f"{hashlib.sha256(uuid.encode()).hexdigest()[:8]}")
    if not MEETING_KEY_RE.match(key):
        raise ValueError(f"meeting_key does not match the contract: {key!r}")
    return MeetingPath(key, r2_folder(key), slug, f"{dt:%d-%m-%Y}")


def file_kind(file_type, recording_type, file_extension=None) -> tuple[str, str]:
    ft, rt = (file_type or "").upper(), slugify(recording_type or "", "_")
    ext = re.sub(r"[^a-z0-9]+", "", (file_extension or "").lower()) or DEFAULT_EXT.get(ft, "bin")
    if ft == "MP4":
        return (f"video-{rt}" if rt else "video"), ext
    return KNOWN_KINDS.get(ft) or rt or slugify(ft, "_") or "file", ext


def assign_file_names(path: MeetingPath, files: list[dict], reserved: dict | None = None) -> dict:
    """zoom_file_id -> file name. Same kind -> -2, -3 by recording_start (ties by id).
    `reserved` (id -> name of already verified files) never changes."""
    names = dict(reserved or {})
    taken = set(names.values())
    for f in sorted(files, key=lambda f: (f.get("recording_start") or "", str(f["id"]))):
        fid = str(f["id"])
        if fid in names:
            continue
        kind, ext = file_kind(f.get("file_type"), f.get("recording_type"), f.get("file_extension"))
        n = 1
        while (name := f"{path.slug}_{path.date}_{kind}{'' if n == 1 else f'-{n}'}.{ext}") in taken:
            n += 1
        taken.add(name)
        names[fid] = name
    return names


# --- teams.yaml and access_groups.json ---------------------------------------------------------

def load_teams(path) -> tuple[str, list]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    rules = [(r["team"], [c.casefold() for c in r.get("contains") or []],
              [re.compile(x, re.I) for x in r.get("regex") or []], {str(m) for m in r.get("meeting_ids") or []})
             for r in data.get("rules") or []]
    return data["default_team"], rules


def match_team(teams, topic, meeting_id) -> tuple[str, bool]:
    """(team, unmapped). First matching rule wins; no match -> default_team, unmapped."""
    default, rules = teams
    t = topic or ""
    for team, contains, regex, ids in rules:
        if str(meeting_id) in ids or any(c in t.casefold() for c in contains) or any(x.search(t) for x in regex):
            return team, False
    return default, True


def load_accounts(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return {k.lower(): v for k, v in (json.load(fh).get("accounts") or {}).items()}
