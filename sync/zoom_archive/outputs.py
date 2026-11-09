"""meeting.json and the status dict (admin page), built from manifest rows."""
from datetime import datetime, timedelta, timezone

from . import __version__
from .naming import cairo_iso, r2_folder


def group(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["meeting_uuid"], []).append(r)
    return out


def is_complete(rows: list[dict]) -> bool:
    return all(r["status"] == "verified" for r in rows)


def folder_of(r: dict) -> str:
    return r2_folder(r["meeting_key"])


def meeting_object(rows: list[dict]) -> dict:
    """meeting.json for one fully verified meeting."""
    rows = sorted(rows, key=lambda r: (r["recording_start"] or "", r["zoom_file_id"]))
    first, folder = rows[0], folder_of(rows[0])
    return {
        "zoom_meeting_id": int(first["meeting_id"]), "zoom_uuid": first["meeting_uuid"],
        "meeting_key": first["meeting_key"], "folder": folder, "topic": first["topic"] or "",
        "account": first["account"], "team": first["team"], "host_email": first["host_email"] or "",
        "start_time_utc": first["start_time_utc"], "start_time_cairo": cairo_iso(first["start_time_utc"]),
        "duration_min": max(0, int(first["duration_min"] or 0)),
        "source": "trash" if any(r["source"] == "trash" for r in rows) else "active",
        "files": [{"name": r["r2_key"][len(folder):], "recording_type": r["recording_type"] or "",
                   "file_type": r["file_type"] or "", "zoom_file_id": r["zoom_file_id"],
                   "zoom_size": int(r["zoom_size"] or 0), "r2_key": r["r2_key"], "r2_size": int(r["r2_size"] or 0),
                   "sha256": r["sha256"], "status": "verified"} for r in rows],
        "archived_at": max(r["updated_at"] or "" for r in rows),  # deterministic across reruns
        "script_version": __version__,
    }


def status_json(rows: list[dict], trash: list[dict], run: dict, known_accounts: set) -> dict:
    now = datetime.now(timezone.utc)
    skipped = [r for r in rows if r["status"] == "skipped"]  # short transcripts: never copied (D16)
    rows = [r for r in rows if r["status"] != "skipped"]
    groups = group(rows)
    verified = [r for r in rows if r["status"] == "verified"]
    accounts: dict[str, dict] = {}
    for r in rows:
        a = accounts.setdefault(r["account"], {"account": r["account"], "host_email": r["host_email"],
                                               "meetings": set(), "bytes": 0, "mapped": r["account"] in known_accounts,
                                               "meetings_complete": 0, "bytes_verified": 0})
        a["meetings"].add(r["meeting_uuid"])
        a["bytes"] += int(r["zoom_size"] or 0)
        a["bytes_verified"] += int(r["zoom_size"] or 0) if r["status"] == "verified" else 0
    for g in groups.values():
        if is_complete(g):
            accounts[g[0]["account"]]["meetings_complete"] += 1
    open_trash = [t for t in trash if not t["archived"] and t["deleted_time"]]
    purge = (min(datetime.fromisoformat(t["deleted_time"].replace("Z", "+00:00")) for t in open_trash)
             + timedelta(days=30)).date() if open_trash else None
    unmapped: dict[str, set] = {}
    for r in rows:
        if r["unmapped"]:
            unmapped.setdefault(r["topic"] or "", set()).add(r["meeting_uuid"])
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_run": run,
        "totals": {"files": len(rows), "verified": len(verified),
                   "failed": sum(r["status"] == "failed" for r in rows),
                   "pending": sum(r["status"] in ("pending", "uploaded") for r in rows),
                   "bytes_verified": sum(int(r["zoom_size"] or 0) for r in verified),
                   "bytes_total": sum(int(r["zoom_size"] or 0) for r in rows),
                   "meetings": len(groups), "meetings_complete": sum(is_complete(g) for g in groups.values()),
                   "skipped_meetings": len({r["meeting_uuid"] for r in skipped}),
                   "skipped_bytes": sum(int(r["zoom_size"] or 0) for r in skipped)},
        "accounts": sorted(({**a, "meetings": len(a["meetings"])} for a in accounts.values()),
                           key=lambda a: -a["bytes"]),
        "trash": {"meetings": len(trash), "bytes": sum(int(t["total_size"] or 0) for t in trash),
                  "archived": sum(bool(t["archived"]) for t in trash),
                  "purge_date": purge.isoformat() if purge else None,
                  "days_left": (purge - now.date()).days if purge else None},
        "failed_files": [{"meeting_key": r["meeting_key"], "topic": r["topic"],
                          "recording_type": r["recording_type"], "last_error": r["last_error"]}
                         for r in rows if r["status"] == "failed"],
        "unmapped_topics": [{"topic": t, "meetings": len(u)} for t, u in sorted(unmapped.items())],
    }
