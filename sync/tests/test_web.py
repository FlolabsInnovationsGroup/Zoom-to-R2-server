"""Admin page + MCP server: access matrix, tools, run control, recordings page, keys, login, Origin check. R2 = moto."""

import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path

import pytest
import requests
from conftest import CONTRACTS, admin_s3
from starlette.testclient import TestClient

from zoom_archive import mcp_server, naming, runctl, search, web
from zoom_archive.access import API_KEY_RE, new_api_key, sha256_hex
from zoom_archive.config import ConfigError, load_web_config
from zoom_archive.manifest import COLS, Manifest
from zoom_archive.vtt import parse_vtt

FX = CONTRACTS / "fixtures"
KEYS = {
    "design": "pm/design/2026/2026-11-04_1400utc_design-team_1d3552d2",
    "frontend": "pm/frontend/2026/2026-11-05_0930utc_weekly-frontend_6d67a695",
    "operations": "pm/operations/2026/2026-11-03_0800utc_mohamed-operations-meeting_4d604a95",
    "buddy": "buddy/misc/2026/2026-07-14_1800utc_meeting-85512345678_e260f2da",
    "humanoid": "john/humanoid/2026/2026-11-08_2130utc_humanoid_6d393ee3",
    "hr": "john/hr/2026/2026-10-20_1000utc_hr-team-meeting_7345cbc9",
}
PASSWORD = "correct horse battery"
ORIGIN = {"Origin": "http://localhost"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def text(name):
    return (FX / name).read_text(encoding="utf-8")


def seed_row(m: dict, f: dict, **over) -> dict:
    """One manifest row (every column in manifest.COLS) from a fixture meeting + file."""
    vals = {"zoom_file_id": f["zoom_file_id"], "meeting_uuid": m["zoom_uuid"], "meeting_id": m["zoom_meeting_id"],
            "host_email": m["host_email"], "topic": m["topic"], "account": m["account"], "team": m["team"],
            "source": m["source"], "recording_type": f["recording_type"], "file_type": f["file_type"],
            "zoom_size": f["zoom_size"], "r2_key": f["r2_key"], "meeting_key": m["meeting_key"],
            "start_time_utc": m["start_time_utc"], "duration_min": m["duration_min"],
            "recording_start": m["start_time_utc"], "restricted": 0, "unmapped": 0, **over}
    return {c: vals[c] for c in COLS}


def seed_manifest(path: str, meetings: list[dict]) -> None:
    db = Manifest(path)
    for m in meetings:
        for f in m["files"]:
            db.upsert_file(seed_row(m, f))
            db.set_status(f["zoom_file_id"], "verified", r2_size=f["r2_size"], sha256=f["sha256"])
    # not archived yet: one pending, one failed, one half done
    base = meetings[0]
    for n, (topic, account, states) in enumerate([("Pending Sync Test", "buddy", ["pending"]),
                                                  ("Failed Sync Test", "john", ["failed"]),
                                                  ("Partial Sync Test", "pm", ["verified", "pending"])]):
        m = {**base, "zoom_uuid": f"extra-{n}", "zoom_meeting_id": 9000 + n, "topic": topic, "account": account,
             "team": "misc", "start_time_utc": f"2026-10-0{n + 1}T10:00:00Z",
             "meeting_key": f"{account}/misc/2026/extra-{n}"}
        for i, st in enumerate(states):
            fid = f"extra-{n}-{i}"
            db.upsert_file(seed_row(m, {**base["files"][0], "zoom_file_id": fid, "zoom_size": 1000 * (i + 1)},
                                    r2_key=f"{account}-account/misc/extra-{n}/f{i}.mp4"))
            db.set_status(fid, st)
    db.c.close()


@pytest.fixture
def web_env(env, monkeypatch, tmp_path, bucket):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ADMIN_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("KEYS_DB_PATH", str(tmp_path / "keys.sqlite"))
    monkeypatch.setattr(web, "FAIL_DELAY", 0)
    s3 = admin_s3()
    put = lambda k, body: s3.put_object(Bucket=bucket, Key=k, Body=body if isinstance(body, bytes) else body.encode())  # noqa: E731
    bodies = {"_transcript.vtt": text("transcript.vtt"), "_summary.json": text("summary.json"),
              "_summary_next_steps.json": text("summary_next_steps.json"), ".m4a": b"audio-bytes"}
    meetings = json.loads(text("all_meetings.json"))
    for m in meetings:
        put(f"{m['folder']}meeting.json", json.dumps(m))
        for f in m["files"]:
            for suffix, body in bodies.items():
                if f["name"].endswith(suffix):
                    put(f["r2_key"], body)
    manifest_path = env["MANIFEST_PATH"]
    seed_manifest(manifest_path, meetings)
    spath = search.default_path(manifest_path)
    for m in meetings:
        if any(f["file_type"] == "TRANSCRIPT" for f in m["files"]):
            search.put_meeting(spath, m["meeting_key"], "transcript", text("transcript.expected.txt"))
    return {"db": str(tmp_path / "keys.sqlite"), "bucket": bucket, "manifest": manifest_path}


@pytest.fixture
def client(web_env):
    app = web.create_app(load_web_config())
    with TestClient(app, base_url="http://localhost") as c:
        c.db, c.bucket, c.manifest = web_env["db"], web_env["bucket"], web_env["manifest"]
        yield c


def make_key(client, groups, revoked_at=None, expires_at=None):
    key, kid = new_api_key(), str(uuid.uuid4())
    db = sqlite3.connect(client.db)
    db.execute("INSERT INTO api_keys (id, name, key_prefix, key_hash, access_groups, created_by, created_at, "
               "expires_at, revoked_at) VALUES (?, 'test', ?, ?, ?, 'x', '2026-11-09T00:00:00Z', ?, ?)",
               (kid, key[:12], sha256_hex(key), json.dumps(groups), expires_at, revoked_at))
    db.commit()
    db.close()
    return key


def call(client, key, name, args, path=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = client.post(path or f"/mcp/{key}", json=body, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res["content"][0]["text"], bool(res.get("isError"))


def can_get(client, key, meeting_key):
    return not call(client, key, "get_meeting", {"meeting_key": meeting_key})[1]


def login(client):
    r = client.post("/admin/login", data={"password": PASSWORD}, headers=ORIGIN)
    assert r.status_code == 200 and "Sync status" in r.text


def admin_post(client, data, origin="http://localhost"):
    return client.post("/admin", data=data, headers={"Origin": origin}, follow_redirects=False)


# ---- access ------------------------------------------------------------------------------------

def test_group_matrix(client):
    member, buddy, adm = (make_key(client, [g]) for g in ("member", "buddy", "admin"))
    order = [KEYS[k] for k in ("design", "frontend", "operations", "buddy", "humanoid", "hr")]
    read = lambda key: [can_get(client, key, k) for k in order]  # noqa: E731
    assert read(member) == [True, True, False, False, False, False]
    assert read(buddy) == [False, False, False, True, False, False]
    assert read(adm) == [True] * 6  # admin reads every account and team, john hr and pm operations included
    listing = call(client, member, "list_meetings", {})[0]
    assert KEYS["design"] in listing and "operations" not in listing
    assert KEYS["hr"] in call(client, adm, "list_meetings", {})[0]


def test_readable_keys():
    from zoom_archive.access import can_read, readable
    assert readable(["admin"], "john-account/hr/2026/f/meeting.json")
    assert readable(["member"], "pm-account/design/2026/f/a.mp4")
    assert not readable(["member"], "pm-account/hr/2026/f/a.mp4")
    assert not readable(["admin"], "_system/manifest.sqlite")
    assert not readable(["admin"], "pm-account/design/2026/../../x")
    assert not readable(["admin"], "pm-account/design/x")  # too short to be a meeting file
    assert not readable(["admin"], "meetings/pm/design/2026/f/a.mp4")
    assert can_read(["admin"], "anything", "any") and not can_read([], "pm", "design")


def test_hidden_meeting_looks_missing(client):
    key = make_key(client, ["member"])
    missing = "pm/operations/2026/2026-01-01_0000utc_nothing_00000000"
    hidden = call(client, key, "get_meeting", {"meeting_key": KEYS["operations"]})
    gone = call(client, key, "get_meeting", {"meeting_key": missing})
    assert hidden[1] and gone[1]
    assert hidden[0].replace(KEYS["operations"], "KEY") == gone[0].replace(missing, "KEY")


def test_revoked_and_expired_keys_get_401(client):
    for extra in ({"revoked_at": "2026-11-01T00:00:00Z"}, {"expires_at": "2026-01-01T00:00:00Z"}):
        key = make_key(client, ["admin"], **extra)
        assert client.post(f"/mcp/{key}", json={}, headers=MCP_HEADERS).status_code == 401
        assert client.post("/mcp", json={}, headers={**MCP_HEADERS, "Authorization": f"Bearer {key}"}).status_code == 401
    assert client.post("/mcp/not-a-key", json={}, headers=MCP_HEADERS).status_code == 401
    assert client.post("/mcp", json={}, headers=MCP_HEADERS).status_code == 401


def test_bearer_header_works_and_every_call_is_logged(client):
    key = make_key(client, ["member"])
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_meeting", "arguments": {"meeting_key": KEYS["design"]}}}
    r = client.post("/mcp", json=body, headers={**MCP_HEADERS, "Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and not r.json()["result"].get("isError")
    call(client, key, "get_meeting", {"meeting_key": KEYS["operations"]})
    call(client, key, "search_transcripts", {"query": "pricing"})
    db = sqlite3.connect(client.db)
    rows = db.execute("SELECT tool, meeting_key, result FROM access_log ORDER BY id").fetchall()
    assert rows == [("get_meeting", KEYS["design"], "ok"), ("get_meeting", KEYS["operations"], "not_found"),
                    ("search_transcripts", None, "ok")]
    assert db.execute("SELECT last_used_at FROM api_keys").fetchone()[0]


def test_group_edit_applies_on_next_call(client):
    key = make_key(client, ["member"])
    assert not can_get(client, key, KEYS["humanoid"])
    login(client)
    kid = sqlite3.connect(client.db).execute("SELECT id FROM api_keys").fetchone()[0]
    assert admin_post(client, {"action": "groups", "id": kid, "groups": ["admin"]}).status_code == 303
    assert can_get(client, key, KEYS["humanoid"])
    assert can_get(client, key, KEYS["design"])  # admin reads everything


# ---- MCP tools -----------------------------------------------------------------------------------

def test_list_accounts_and_teams(client):
    out = call(client, make_key(client, ["member"]), "list_accounts_and_teams", {})[0]
    assert "pm/design | 1 | 2026-11-04 | 2026-11-04" in out and "pm/frontend" in out
    assert "operations" not in out and "john" not in out and "buddy" not in out
    assert "Partial" not in out  # meetings that are not fully verified stay out
    adm = call(client, make_key(client, ["admin"]), "list_accounts_and_teams", {})[0]
    assert all(t in adm for t in ("pm/operations", "john/hr", "john/humanoid", "buddy/misc"))
    assert "buddy/misc | 1 |" in adm  # the pending buddy meeting is not counted


def test_list_meetings_filters(client):
    adm = make_key(client, ["admin"])
    nov = call(client, adm, "list_meetings", {"month": "2026-11"})[0]
    assert all(KEYS[k] in nov for k in ("design", "frontend", "operations", "humanoid"))
    assert KEYS["buddy"] not in nov and KEYS["hr"] not in nov
    assert "transcript, summary" in nov  # the humanoid meeting has both
    year = call(client, adm, "list_meetings", {"year": "2026", "limit": 2})[0]
    assert "next_cursor: 2" in year and "6 meeting(s)" in year
    assert KEYS["buddy"] in call(client, adm, "list_meetings", {"account": "buddy"})[0]
    assert KEYS["design"] in call(client, adm, "list_meetings", {"query": "design team"})[0]
    assert "No meetings found" in call(client, adm, "list_meetings", {"month": "2020-01"})[0]


def test_search_transcripts_finds_and_respects_groups(client):
    member, adm = make_key(client, ["member"]), make_key(client, ["admin"])
    out = call(client, member, "search_transcripts", {"query": "pricing"})[0]
    assert KEYS["design"] in out and KEYS["frontend"] in out
    assert "[00:00:01]" in out and "**pricing**" in out and "UNTRUSTED MEETING CONTENT" in out
    assert not any(KEYS[k] in out for k in ("operations", "buddy", "humanoid", "hr"))
    full = call(client, adm, "search_transcripts", {"query": "pricing"})[0]
    assert all(KEYS[k] in full for k in KEYS if k != "operations") and KEYS["operations"] in full
    only = call(client, adm, "search_transcripts", {"query": "pricing", "account": "buddy"})[0]
    assert KEYS["buddy"] in only and KEYS["design"] not in only
    assert "No matches" in call(client, member, "search_transcripts", {"query": "zzzqqq"})[0]


def test_find_in_transcript(client):
    key = make_key(client, ["member"])
    out = call(client, key, "find_in_transcript", {"meeting_key": KEYS["design"], "query": "contrast",
                                                    "context_lines": 0})[0]
    assert "Contrast is fixed" in out and "Good morning" not in out and "1 matching line" in out
    near = call(client, key, "find_in_transcript", {"meeting_key": KEYS["design"], "query": "contrast"})[0]
    assert "hero section" in near  # one neighbour line before
    assert "No line" in call(client, key, "find_in_transcript", {"meeting_key": KEYS["design"], "query": "banana"})[0]
    assert call(client, key, "find_in_transcript", {"meeting_key": KEYS["hr"], "query": "contrast"})[1]  # hidden


def test_get_transcript_time_window(client):
    key = make_key(client, ["member"])
    t = call(client, key, "get_transcript", {"meeting_key": KEYS["design"], "start": "00:00:05",
                                              "end": "00:00:09"})[0]
    assert "hero section" in t and "pricing cards" in t
    assert "Good morning" not in t and "[00:01:15]" not in t
    assert "No transcript lines" in call(client, key, "get_transcript", {
        "meeting_key": KEYS["design"], "start": "05:00:00"})[0]


def test_get_summary(client):
    adm = make_key(client, ["admin"])
    out = call(client, adm, "get_summary", {"meeting_key": KEYS["humanoid"]})[0]
    assert "The team reviewed humanoid gait tests" in out and "Gait test results" in out
    assert "retune ankle controller" in out and "UNTRUSTED MEETING CONTENT" in out
    none = call(client, adm, "get_summary", {"meeting_key": KEYS["design"]})
    assert "no AI summary" in none[0] and not none[1]
    assert call(client, make_key(client, ["member"]), "get_summary", {"meeting_key": KEYS["humanoid"]})[1]


def test_get_meeting_lists_files_without_content(client):
    out = call(client, make_key(client, ["admin"]), "get_meeting", {"meeting_key": KEYS["humanoid"]})[0]
    assert "summary: yes" in out and "transcript: yes" in out and "_transcript.vtt | TRANSCRIPT" in out
    assert "retune ankle controller" not in out and "r2_key" not in out and "sha256" not in out
    plain = call(client, make_key(client, ["member"]), "get_meeting", {"meeting_key": KEYS["design"]})[0]
    assert "summary: no" in plain and "MB" in plain


def test_vtt_fixture_and_transcript_tool(client):
    expected = text("transcript.expected.txt")
    assert parse_vtt(text("transcript.vtt")) == expected
    t = call(client, make_key(client, ["member"]), "get_transcript", {"meeting_key": KEYS["design"]})[0]
    assert expected in t and "UNTRUSTED MEETING CONTENT" in t


def test_pages_join_back():
    body = "".join(f"[00:00:{i % 60:02d}] Speaker: line number {i}\n" for i in range(3000))
    starts = mcp_server.page_starts(body)
    pages = [body[s:(starts[i + 1] if i + 1 < len(starts) else len(body))] for i, s in enumerate(starts)]
    assert len(starts) > 2 and all(len(p) <= mcp_server.PAGE_MAX_CHARS for p in pages)
    assert "".join(pages) == body


def test_recording_links_are_presigned_and_group_checked(client):
    member, adm = make_key(client, ["member"]), make_key(client, ["admin"])
    out = call(client, member, "get_recording_links", {"meeting_key": KEYS["design"], "kind": "audio"})[0]
    url = re.search(r"https?://\S+", out).group(0)
    assert "X-Amz-Signature" in url and "_audio.m4a" in out and ".mp4" not in out
    assert requests.get(url, timeout=10).content == b"audio-bytes"
    everything = call(client, adm, "get_recording_links", {"meeting_key": KEYS["design"]})[0]
    assert everything.count("X-Amz-Signature") == 5
    hidden = call(client, member, "get_recording_links", {"meeting_key": KEYS["operations"]})
    assert hidden[1] and "X-Amz" not in hidden[0]
    ro = web.ReadOnlyR2(load_web_config()["r2"]).for_groups(["member"])
    assert ro.presign(naming.r2_folder(KEYS["operations"]) + "x.m4a") is None


def test_search_and_fetch(client):
    key = make_key(client, ["member"])
    hits = json.loads(call(client, key, "search", {"query": "design"})[0])["results"]
    assert hits[0]["id"] == KEYS["design"]
    by_content = json.loads(call(client, key, "search", {"query": "pricing cards"})[0])["results"]
    assert {h["id"] for h in by_content} == {KEYS["design"], KEYS["frontend"]}
    doc = json.loads(call(client, key, "fetch", {"id": hits[0]["url"]})[0])
    assert doc["id"] == KEYS["design"] and "UNTRUSTED MEETING CONTENT" in doc["text"]


# ---- admin ---------------------------------------------------------------------------------------

def test_admin_needs_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert client.get("/admin/recordings", follow_redirects=False).headers["location"] == "/admin/login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/admin"
    assert admin_post(client, {"action": "revoke", "id": "x"}).status_code == 403
    assert admin_post(client, {"action": "start_backfill"}).status_code == 403


def test_wrong_password_is_refused(client):
    r = client.post("/admin/login", data={"password": "nope"}, headers=ORIGIN)
    assert r.status_code == 401 and "Wrong password" in r.text
    assert client.get("/admin", follow_redirects=False).status_code == 303
    r = client.post("/admin/login", data={"password": PASSWORD}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie.replace("samesite", "")


def test_post_needs_matching_origin(client):
    assert client.post("/admin/login", data={"password": PASSWORD},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/admin/login", data={"password": PASSWORD}).status_code == 403
    login(client)
    assert admin_post(client, {"action": "revoke", "id": "x"}, "https://evil.example").status_code == 403
    assert admin_post(client, {"action": "stop"}, "https://evil.example").status_code == 403
    assert client.post("/admin", data={"action": "revoke", "id": "x"}).status_code == 403


def test_codespace_address_is_trusted(web_env, monkeypatch):
    monkeypatch.setenv("CODESPACE_NAME", "fluffy-robot")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    cfg = load_web_config()
    assert cfg["origin"] == "https://fluffy-robot-8080.app.github.dev"
    with TestClient(web.create_app(cfg), base_url="http://localhost") as c:
        ok = c.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False,
                    headers={"Origin": "https://fluffy-robot-8080.app.github.dev"})
        assert ok.status_code == 303
        assert c.post("/admin/login", data={"password": PASSWORD},
                      headers={"Origin": "https://other-8080.app.github.dev"}).status_code == 403


def test_logout(client):
    login(client)
    assert client.post("/admin/logout", headers=ORIGIN, follow_redirects=False).status_code == 303
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_session_dies_when_password_changes(client, web_env, monkeypatch):
    login(client)
    monkeypatch.setenv("ADMIN_PASSWORD", "a different password")
    app2 = web.create_app(load_web_config())
    with TestClient(app2, base_url="http://localhost") as c2:
        c2.cookies.update(client.cookies)
        assert c2.get("/admin", follow_redirects=False).status_code == 303


def test_status_is_live_from_manifest(client, env, monkeypatch, tmp_path):
    login(client)
    page = client.get("/admin").text
    assert "pm-account/" in page and "john-account/" in page and "buddy-account/" in page
    assert re.search(r"\d+ of \d+ meetings \(\d+% of bytes\)", page) and "All recordings" in page
    db = Manifest(env["MANIFEST_PATH"])
    db.x("INSERT INTO trash_items (meeting_uuid, meeting_id, topic, total_size, deleted_time, first_seen_at, "
         "last_seen_at) VALUES ('u1', 1, 't', 100, '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', '2026-11-02T00:00:00Z')")
    db.c.close()
    assert "John must click Recover in the Zoom portal" in client.get("/admin").text
    monkeypatch.setenv("MANIFEST_PATH", str(tmp_path / "nothing" / "manifest.sqlite"))
    with TestClient(web.create_app(load_web_config()), base_url="http://localhost") as c2:
        c2.cookies.update(client.cookies)
        assert "The sync has not run yet" in c2.get("/admin").text


def test_create_key_shows_once_and_stores_hash_only(client):
    login(client)
    r = admin_post(client, {"action": "create", "name": "Ahsan CLI", "groups": ["member", "buddy"]})
    assert r.status_code == 200
    key = re.search(r"fma_[A-Za-z0-9_-]{43}", r.text).group(0)
    assert API_KEY_RE.match(key)
    assert f"claude mcp add --transport http flolabs-meetings http://localhost/mcp/{key}" in r.text
    assert f"<pre id=\"u\">http://localhost/mcp/{key}</pre>" in r.text
    db = sqlite3.connect(client.db)
    row = db.execute("SELECT * FROM api_keys WHERE name = 'Ahsan CLI'").fetchone()
    assert sha256_hex(key) in row and key not in json.dumps(row)
    assert key not in client.get("/admin").text
    assert can_get(client, key, KEYS["design"])
    assert admin_post(client, {"action": "create", "name": "x"}).status_code == 400  # no group ticked


def test_revoked_key_switches_are_disabled(client):
    login(client)
    make_key(client, ["member"], revoked_at="2026-11-01T00:00:00Z")
    page = client.get("/admin").text
    assert '<button disabled>Save access</button>' in page
    assert 'name="groups" value="member" checked disabled' in page
    make_key(client, ["buddy"])
    assert page.count("Save access") == 1 and client.get("/admin").text.count("<button>Save access</button>") == 1


def test_key_vectors():
    for v in json.loads((CONTRACTS / "key_vectors.json").read_text(encoding="utf-8"))["vectors"]:
        assert API_KEY_RE.match(v["key"]) and v["key"][:12] == v["key_prefix"]
        assert sha256_hex(v["key"]) == v["sha256"]


# ---- run control ---------------------------------------------------------------------------------

def test_start_scan_and_stop_buttons(client, monkeypatch):
    login(client)
    started = []
    monkeypatch.setattr(runctl, "start", lambda path, mode, *a: started.append(mode) or True)
    assert admin_post(client, {"action": "start_backfill"}).status_code == 303
    assert admin_post(client, {"action": "start_scan"}).status_code == 303
    assert started == ["backfill", "scan"]
    monkeypatch.setattr(runctl, "start", lambda path, mode, *a: False)
    r = admin_post(client, {"action": "start_backfill"})
    assert r.status_code == 409 and "A run is already going" in r.text
    idle = client.get("/admin").text
    assert re.search(r">(Idle|Done|Paused)</span>", idle) and "Scan Zoom" in idle and "data-live" not in idle


def test_stop_writes_request_only_while_running(client):
    login(client)
    stop_file = Path(client.manifest).with_name("sync.stop")
    assert admin_post(client, {"action": "stop"}).status_code == 303
    assert not stop_file.exists()
    runctl.heartbeat(client.manifest, {"pid": 1, "mode": "backfill", "started_at": "2026-11-09T10:00:00Z"})
    live = client.get("/admin").text
    assert ">Copying</span>" in live and "data-live" in live
    assert admin_post(client, {"action": "stop"}).status_code == 303
    assert stop_file.exists()
    assert "Stopping..." in client.get("/admin").text


def test_run_card_shows_escaped_log_tail(client):
    login(client)
    Path(client.manifest).with_name("sync.log").write_text("line one\n<script>alert(1)</script>\n", encoding="utf-8")
    page = client.get("/admin").text
    assert "line one" in page and "<script>alert(1)</script>" not in page and "&#60;script&#62;" in page


# ---- recordings page -----------------------------------------------------------------------------

def test_recordings_page_lists_everything_and_filters(client):
    login(client)
    page = client.get("/admin/recordings").text
    assert "9 meetings" in page and "6 archived, 2 pending, 1 failed" in page
    for topic in ("Design Team", "Pending Sync Test", "Failed Sync Test", "Partial Sync Test", "Humanoid"):
        assert topic in page
    assert page.index("Humanoid") < page.index("Design Team")  # newest first
    assert 'class="tag ok">Archived' in page and 'class="tag bad">Failed' in page and 'class="tag warn">Pending (partial)' in page
    failed = client.get("/admin/recordings?status=failed").text
    assert "Failed Sync Test" in failed and "Design Team" not in failed
    pending = client.get("/admin/recordings?status=pending").text
    assert "Pending Sync Test" in pending and "Partial Sync Test" in pending and "Failed Sync Test" not in pending
    buddy = client.get("/admin/recordings?account=buddy&status=archived").text
    assert "اجتماع الفريق" in buddy and "Design Team" not in buddy and "Pending Sync Test" not in buddy
    assert "style=" not in page  # CSP: no inline styles
    assert "Showing 1-9 of 9" in page and 'data-label="Topic"' in page
    q = client.get("/admin/recordings?q=humanoid").text
    assert "Showing 1-1 of 1" in q and "Humanoid" in q and "Design Team" not in q


# ---- startup and logging ----------------------------------------------------------------------------

def test_refuses_to_start_without_admin_password(web_env, monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_PASSWORD")
    with pytest.raises(ConfigError, match="ADMIN_PASSWORD"):
        web.create_app()
    assert web.main() == 2
    assert "ADMIN_PASSWORD" in capsys.readouterr().err


def test_key_never_reaches_logs(client, caplog, monkeypatch):
    key = make_key(client, ["member"])
    caplog.set_level(logging.DEBUG)
    call(client, key, "list_meetings", {})
    client.post(f"/mcp/{key[:-3]}xyz", json={}, headers=MCP_HEADERS)  # a wrong key must not be logged either
    ours = [r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert ours and not any(key in m or key[4:] in m for m in ours)
    # the real server: uvicorn's access log (which prints paths) is switched off
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert web.main() == 0 and seen["access_log"] is False


# ---- D15 review fixes ------------------------------------------------------------------------------

def test_member_is_an_allow_list_so_unknown_and_restricted_teams_stay_hidden():
    from zoom_archive.access import can_read
    for team in ("misc", "hr", "one-on-one", "operations", "project-management", "ethics-legal", "some-new-team"):
        assert not can_read(["member"], "pm", team)
    assert can_read(["member"], "pm", "design") and not can_read(["member"], "john", "design")
    teams = naming.load_teams(Path(__file__).resolve().parents[1] / "teams.yaml")
    for topic, want in (("Ethics & Legal - Automation policy", "ethics-legal"),
                        ("Design Team / Project Management sync", "project-management"),
                        ("Multi-modal perception operations", "operations")):
        assert naming.match_team(teams, topic, 1)[0] == want
    assert not can_read(["member"], "pm", naming.match_team(teams, "HR interview", 1)[0])  # -> misc


def test_unmapped_host_never_gets_a_mapped_account_name():
    accounts = naming.load_accounts(CONTRACTS / "access_groups.json")
    for email in ("pm@anotherdomain.com", "buddy@x.com", "john@x.com"):
        assert naming.account_slug(email, accounts).startswith("unmapped-")
    assert naming.account_slug("PM@flomad.com", accounts) == "pm"


def test_login_locks_after_repeated_failures(client):
    for _ in range(web.MAX_FAILS):
        assert client.post("/admin/login", data={"password": "nope"}, headers=ORIGIN).status_code == 401
    r = client.post("/admin/login", data={"password": PASSWORD}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 429  # even the right password waits


def test_logout_ends_a_copied_cookie_and_cookie_has_no_password_hash(client):
    login(client)
    cookie = client.cookies.get("fl_session")
    assert sha256_hex(PASSWORD)[:16] not in cookie
    client.cookies.set("fl_session", cookie)
    assert client.get("/admin", follow_redirects=False).status_code == 200
    assert client.post("/admin/logout", headers=ORIGIN, follow_redirects=False).status_code == 303
    client.cookies.set("fl_session", cookie)  # the thief replays it
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_web_uses_the_read_only_r2_token_when_given(web_env, monkeypatch):
    assert load_web_config()["r2"].r2_access_key_id != "ro-id"
    monkeypatch.setenv("R2_READ_ACCESS_KEY_ID", "ro-id")
    monkeypatch.setenv("R2_READ_SECRET_ACCESS_KEY", "ro-secret")
    cfg = load_web_config()["r2"]
    assert (cfg.r2_access_key_id, cfg.r2_secret_access_key) == ("ro-id", "ro-secret")


def test_presigned_links_are_short_lived():
    assert mcp_server.LINK_SECONDS <= 900


def test_disabled_buttons_stay_readable_on_hover():
    from zoom_archive import admin_page
    assert "button:disabled:hover" in admin_page.ADMIN_CSS and "button:disabled:hover{background:var(--card)}" not in admin_page.ADMIN_CSS


def test_stopping_run_disables_stop_button(client):
    login(client)
    runctl.heartbeat(client.manifest, {"pid": 1, "mode": "backfill", "started_at": "2026-11-09T10:00:00Z"})
    Path(client.manifest).with_name("sync.stop").write_text("1")
    page = client.get("/admin").text
    assert re.search(r"<button class=\"danger\" disabled>Stopping\.\.\.</button>", page) and "Stop the current run" in page


# ---- runctl: one run at a time ---------------------------------------------------------------------

def test_claim_is_exclusive_and_finished_only_removes_own_files(tmp_path):
    import os
    m = str(tmp_path / "manifest.sqlite")
    info = {"pid": os.getpid(), "mode": "backfill", "started_at": "2026-11-09T10:00:00Z"}
    runctl.heartbeat(m, {**info, "pid": os.getpid() + 1})
    assert runctl.claim(m, info) is False  # someone else runs
    runctl.finished(m)  # not ours: must stay
    assert runctl.running(m)
    Path(m).with_name("sync.alive").unlink()
    Path(m).with_name("sync.stop").write_text("1")  # left over from a killed run
    assert runctl.claim(m, info) is True and not Path(m).with_name("sync.stop").exists()
    assert runctl.claim(m, info) is True  # own pid (registered by start()) counts as ours
    runctl.finished(m)
    assert runctl.running(m) is None


def test_double_start_launches_one_child(tmp_path, monkeypatch):
    import subprocess
    import threading
    m = str(tmp_path / "manifest.sqlite")
    launched = []

    class Child:
        pid = 4242

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(1) or Child())
    results = []
    ts = [threading.Thread(target=lambda: results.append(runctl.start(m, "backfill"))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(launched) == 1 and results.count(True) == 1
