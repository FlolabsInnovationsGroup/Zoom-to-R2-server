"""Full-text search over transcripts and AI summaries (D15). SQLite FTS5 in SEARCH_DB_PATH (search.sqlite).

The sync writes (put_meeting) after a TRANSCRIPT or SUMMARY file is verified; `--mode reindex` rebuilds the
whole file from R2. The MCP server only reads (query). One row = one chunk of transcript lines (about
CHUNK_CHARS), so a hit carries its own [HH:MM:SS] start time and stays short.
"""
import re
import sqlite3
from pathlib import Path

CHUNK_CHARS = 600
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
  meeting_key UNINDEXED, kind UNINDEXED, at UNINDEXED, body,
  tokenize = 'unicode61 remove_diacritics 2');
"""
TIME_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")


def default_path(manifest_path: str) -> str:
    return str(Path(manifest_path).with_name("search.sqlite"))


def connect(path: str, readonly: bool = False) -> sqlite3.Connection | None:
    """None when read-only and the file does not exist yet (nothing indexed)."""
    p = Path(path)
    if readonly:
        if not p.is_file():
            return None
        c = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(p), timeout=30)
        c.executescript(SCHEMA)
    c.row_factory = sqlite3.Row
    return c


def chunk_transcript(text: str) -> list[tuple[str, str]]:
    """'[HH:MM:SS] Speaker: text' lines -> [(start time, chunk text)], each chunk about CHUNK_CHARS."""
    out, buf, at = [], [], ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if not buf:
            m = TIME_RE.match(line)
            at = m.group(1) if m else ""
        buf.append(line)
        if sum(len(x) + 1 for x in buf) >= CHUNK_CHARS:
            out.append((at, "\n".join(buf)))
            buf = []
    if buf:
        out.append((at, "\n".join(buf)))
    return out


def put_meeting(path: str, meeting_key: str, kind: str, text: str) -> None:
    """Replace the `kind` ('transcript' or 'summary') rows of one meeting."""
    c = connect(path)
    try:
        with c:
            c.execute("DELETE FROM chunks WHERE meeting_key = ? AND kind = ?", (meeting_key, kind))
            rows = chunk_transcript(text) if kind == "transcript" else [("", text)]
            c.executemany("INSERT INTO chunks (meeting_key, kind, at, body) VALUES (?,?,?,?)",
                          [(meeting_key, kind, at, body) for at, body in rows if body.strip()])
    finally:
        c.close()


def match_expr(query: str, any_word: bool = False) -> str:
    """User text -> a safe FTS5 expression: every word quoted (no FTS syntax gets through)."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1][:12]
    return (" OR " if any_word else " ").join(f'"{w}"' for w in words)


def query(path: str, text: str, allowed, limit: int = 10, per_meeting: int = 3, scan: int = 2000) -> list[dict]:
    """Best meetings for `text`, only those for which allowed(meeting_key) is True.
    [{meeting_key, hits: [{kind, at, snippet}]}], best first. All words must match; if nothing does, any word."""
    c = connect(path, readonly=True)
    if c is None:
        return []
    try:
        for any_word in (False, True):
            expr = match_expr(text, any_word)
            if not expr:
                return []
            rows = c.execute(
                "SELECT meeting_key, kind, at, snippet(chunks, 3, '**', '**', ' ... ', 24) AS snip "
                "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?", (expr, scan)).fetchall()
            out: dict[str, list] = {}
            for r in rows:
                if not allowed(r["meeting_key"]):
                    continue
                hits = out.setdefault(r["meeting_key"], [])
                if len(hits) < per_meeting:
                    hits.append({"kind": r["kind"], "at": r["at"], "snippet": " ".join(r["snip"].split())})
            if out:
                return [{"meeting_key": k, "hits": v} for k, v in list(out.items())[:limit]]
        return []
    except sqlite3.Error:
        return []
    finally:
        c.close()
