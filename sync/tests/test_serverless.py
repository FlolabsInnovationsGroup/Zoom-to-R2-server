"""Serverless (Vercel) mode: SQLite files in R2, keys saved with conditional writes, access log objects,
the daily cron endpoint and the sync lock. R2 = moto, Zoom = the fake server."""
import json
import re
import sqlite3

import pytest
from conftest import admin_s3
from starlette.testclient import TestClient
from test_archive import three_users
from test_web import MCP_HEADERS, ORIGIN, PASSWORD, admin_post, web_env  # noqa: F401 - web_env is a fixture

from zoom_archive import cli, cloud, r2db, web
from zoom_archive.access import API_KEY_RE
from zoom_archive.config import load_config, load_web_config
from zoom_archive.r2 import R2, Conflict

SECRET = "cron-secret-for-tests-1234"


def r2_keys(bucket, prefix):
    r = admin_s3().list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [o["Key"] for o in r.get("Contents", [])]


def r2_sqlite(bucket, key, tmp_path, sql):
    f = tmp_path / f"check-{key.replace('/', '_')}"
    f.write_bytes(admin_s3().get_object(Bucket=bucket, Key=key)["Body"].read())
    c = sqlite3.connect(f)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


@pytest.fixture
def cloud_env(web_env, monkeypatch, tmp_path, capsys):
    """The laptop's files uploaded once with upload-db, then the app switched to DB_IN_R2=1."""
    from zoom_archive.access import init_keys_db
    init_keys_db(web_env["db"])
    assert cli.main(["--mode", "upload-db"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("DB_IN_R2", "1")
    monkeypatch.setenv("ZOOM_ARCHIVE_TMP", str(tmp_path / "fn-tmp"))
    monkeypatch.setenv("CRON_SECRET", SECRET)
    monkeypatch.setattr(r2db, "_state", {})
    monkeypatch.setattr(r2db, "_log_cache", {"at": 0.0, "rows": [], "last_used": {}})
    monkeypatch.setattr(r2db, "CHECK_EVERY", 0)  # always re-check R2 in tests
    return web_env


@pytest.fixture
def cclient(cloud_env):
    with TestClient(web.create_app(load_web_config()), base_url="http://localhost") as c:
        c.bucket = cloud_env["bucket"]
        yield c


def test_upload_db_puts_files_in_r2_and_never_overwrites(web_env, capsys):
    from zoom_archive.access import init_keys_db
    init_keys_db(web_env["db"])
    assert cli.main(["--mode", "upload-db"]) == 0
    first = capsys.readouterr().out
    assert "_system/keys.sqlite: uploaded" in first and "_system/search.sqlite: uploaded" in first
    assert cli.main(["--mode", "upload-db"]) == 0
    assert capsys.readouterr().out.count("already has it") == 3


def test_keys_are_saved_to_r2_and_mcp_calls_are_logged(cclient, tmp_path):
    r = cclient.post("/admin/login", data={"password": PASSWORD}, headers=ORIGIN)
    assert r.status_code == 200 and "Check Zoom now" in r.text and "Scan Zoom" not in r.text
    r = admin_post(cclient, {"action": "create", "name": "Cloud key", "groups": ["admin"]})
    assert r.status_code == 303
    r = cclient.get("/admin")
    key = API_KEY_RE.search(r.text).group(0) if API_KEY_RE.search(r.text) else re.search(r"fma_[\w-]{43}", r.text).group(0)
    rows = r2_sqlite(cclient.bucket, "_system/keys.sqlite", tmp_path, "SELECT name, revoked_at FROM api_keys")
    assert ("Cloud key", None) in rows  # the key list lives in R2, not on the function's disk

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_meetings", "arguments": {}}}
    r = cclient.post(f"/mcp/{key}", json=body, headers=MCP_HEADERS)
    assert r.status_code == 200 and not r.json()["result"].get("isError"), r.text
    logs = r2_keys(cclient.bucket, r2db.LOG_PREFIX)
    assert len(logs) == 1 and json.loads(admin_s3().get_object(Bucket=cclient.bucket, Key=logs[0])["Body"].read())[
        "tool"] == "list_meetings"
    page = cclient.get("/admin").text
    assert "Cloud key" in page and "list_meetings" in page

    kid = r2_sqlite(cclient.bucket, "_system/keys.sqlite", tmp_path, "SELECT id FROM api_keys WHERE name='Cloud key'")[0][0]
    assert admin_post(cclient, {"action": "revoke", "id": kid}).status_code == 303
    assert cclient.post(f"/mcp/{key}", json=body, headers=MCP_HEADERS).status_code == 401


def test_a_clash_on_the_key_list_is_retried_not_lost(cloud_env, tmp_path):
    r2 = R2(load_config())
    r2db.pull(r2, "keys", fresh=True)
    calls = []

    def add(name):
        def change(path):
            calls.append(name)
            if len(calls) == 1:  # someone else saves the key list while we are editing ours
                def other(p):
                    c = sqlite3.connect(p)
                    c.execute("INSERT INTO api_keys (id, name, key_prefix, key_hash, access_groups, created_by, "
                              "created_at) VALUES ('other', 'Other', 'fma_x', 'h2', '[\"admin\"]', 'x', 'now')")
                    c.commit()
                    c.close()
                    return p
                data_path = tmp_path / "other.sqlite"
                data_path.write_bytes(admin_s3().get_object(Bucket=cloud_env["bucket"], Key=r2db.KEYS["keys"])["Body"].read())
                other(str(data_path))
                admin_s3().put_object(Bucket=cloud_env["bucket"], Key=r2db.KEYS["keys"], Body=data_path.read_bytes())
            c = sqlite3.connect(path)
            c.execute("INSERT INTO api_keys (id, name, key_prefix, key_hash, access_groups, created_by, created_at) "
                      f"VALUES ('{name}', '{name}', 'fma_y', 'h-{name}', '[\"admin\"]', 'x', 'now')")
            c.commit()
            c.close()
        return change

    r2db.edit(r2, "keys", add("mine"))
    names = {n for (n,) in r2_sqlite(cloud_env["bucket"], r2db.KEYS["keys"], tmp_path, "SELECT name FROM api_keys")}
    assert {"Other", "mine"} <= names and len(calls) == 2  # retried on top of the other save; both kept


def test_write_if_refuses_a_stale_etag(cloud_env):
    r2 = R2(load_config())
    first = r2.write_if("_system/test-cond.json", b"1", None)
    with pytest.raises(Conflict):
        r2.write_if("_system/test-cond.json", b"2", None)  # exists already
    r2.write_if("_system/test-cond.json", b"3", first)
    with pytest.raises(Conflict):
        r2.write_if("_system/test-cond.json", b"4", first)  # first is stale now


def test_cron_needs_the_secret(cclient):
    assert cclient.get("/api/cron/sync").status_code == 401
    assert cclient.get("/api/cron/sync", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_cron_runs_a_sync_and_saves_progress_to_r2(cclient, fake_zoom, tmp_path):
    three_users(fake_zoom)
    r = cclient.get("/api/cron/sync", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["ok"] and got["files"] > 0 and not got.get("error"), got
    lock = json.loads(admin_s3().get_object(Bucket=cclient.bucket, Key=r2db.LOCK_KEY)["Body"].read())
    assert lock["running"] is False and lock["last"]["files"] == got["files"]
    verified = r2_sqlite(cclient.bucket, "_system/manifest.sqlite", tmp_path,
                         "SELECT COUNT(*) FROM files WHERE status = 'verified'")[0][0]
    assert verified >= got["files"]
    cclient.post("/admin/login", data={"password": PASSWORD}, headers=ORIGIN)
    assert "Last check" in cclient.get("/admin").text


def test_a_second_sync_waits_for_the_first(cloud_env):
    r2 = R2(load_config())
    assert r2db.take_lock(r2, "someone")
    assert cloud.run_sync("cron") == {"ok": False, "busy": True, "message": "Another sync is already running."}


def test_deadline_leaves_files_for_the_next_run(cloud_env, fake_zoom, monkeypatch):
    three_users(fake_zoom)
    monkeypatch.setattr(cloud, "deadline_seconds", lambda: -1)  # already past: start nothing
    got = cloud.run_sync("cron")
    assert got["ok"] and got["files"] == 0 and got["left"] > 0, got


def test_a_wrong_teams_yaml_path_falls_back_to_the_bundled_rules(env, monkeypatch):
    monkeypatch.setenv("TEAMS_YAML", "/opt/zoom-archive/app/sync/teams.yaml")  # a server path copied into Vercel
    cfg = load_config()
    assert cfg.teams_yaml.endswith("teams.yaml") and not cfg.teams_yaml.startswith("/opt")


def test_sync_finds_the_zoom_users_without_zoom_user_emails(cloud_env, fake_zoom, monkeypatch):
    three_users(fake_zoom)
    fake_zoom.users_scope_error = True  # the real app cannot list users (4711)
    monkeypatch.delenv("ZOOM_USER_EMAILS", raising=False)  # and the setting is missing, as on Vercel
    got = cloud.run_sync("cron")
    assert got["ok"] and got["files"] > 0 and not got.get("error"), got


def test_progress_is_saved_to_r2_while_a_run_is_copying(cloud_env, fake_zoom, monkeypatch, tmp_path):
    three_users(fake_zoom)
    monkeypatch.setattr(cloud, "SAVE_EVERY", 0.05)
    saves = []
    real_write = R2.write

    def counting_write(self, key, data):
        if key == r2db.KEYS["manifest"]:
            saves.append(len(data))
        return real_write(self, key, data)

    monkeypatch.setattr(R2, "write", counting_write)
    got = cloud.run_sync("cron")
    assert got["ok"] and got["files"] > 0
    assert len(saves) >= 2  # during the run, and the final state at the end


def test_a_running_sync_keeps_its_lock_fresh(cloud_env):
    r2 = R2(load_config())
    token = r2db.take_lock(r2, "cron")
    old = json.loads(admin_s3().get_object(Bucket=cloud_env["bucket"], Key=r2db.LOCK_KEY)["Body"].read())
    admin_s3().put_object(Bucket=cloud_env["bucket"], Key=r2db.LOCK_KEY,
                          Body=json.dumps({**old, "since": old["since"] - 3600}).encode())  # looks an hour old
    assert r2db.renew_lock(r2, token)
    assert r2db.take_lock(r2, "admin") is None  # renewed, so a second run still waits
    assert not r2db.renew_lock(r2, "someone-else")


def test_live_progress_is_published_while_copying(cloud_env, fake_zoom, monkeypatch):
    three_users(fake_zoom)
    monkeypatch.setattr(cloud, "PROGRESS_EVERY", 0.02)
    seen = []
    real = r2db.renew_lock

    def spy(r2, token, progress=None):
        if progress:
            seen.append(progress)
        return real(r2, token, progress)

    monkeypatch.setattr(r2db, "renew_lock", spy)
    got = cloud.run_sync("cron")
    assert got["ok"] and got["files"] > 0
    copying = [p for p in seen if p["phase"] == "copying" and p["files_total"]]
    assert copying and copying[-1]["files_total"] >= got["files"]
    assert all(p["bytes_done"] <= p["bytes_total"] for p in copying)


def test_admin_card_shows_a_progress_bar_while_copying():
    from zoom_archive import admin_page
    lock = {"running": True, "progress": {"phase": "copying", "files_total": 47, "files_done": 12, "bytes_total": 7e9,
            "bytes_done": 1.4e9, "copy_started": 100.0, "at": 130.0,
            "current": [{"topic": "Robotics Team meeting", "type": "MP4", "pct": 63}]}}
    html = admin_page._cloud_card(lock, {"files": 10})
    assert "12 of 47 new files" in html and "<progress" in html and "Robotics Team meeting" in html and "video" in html
    assert "stopped unexpectedly" in admin_page._cloud_card({"running": False, "died": True}, {"files": 10})


def test_a_dead_runs_lock_frees_itself_after_two_minutes(cloud_env):
    r2 = R2(load_config())
    assert r2db.take_lock(r2, "cron")
    old = json.loads(admin_s3().get_object(Bucket=cloud_env["bucket"], Key=r2db.LOCK_KEY)["Body"].read())
    admin_s3().put_object(Bucket=cloud_env["bucket"], Key=r2db.LOCK_KEY,
                          Body=json.dumps({**old, "since": old["since"] - 3 * 60}).encode())  # silent for 3 minutes
    assert r2db.lock_state(r2).get("died") is True
    assert r2db.take_lock(r2, "admin")  # the next check can start


def test_health_says_which_commit_is_deployed(cclient, monkeypatch):
    assert cclient.get("/api/health").json() == {"ok": True, "commit": "local"}


def test_a_sync_that_cannot_start_reports_why(cloud_env, monkeypatch):
    monkeypatch.delenv("ZOOM_CLIENT_SECRET")
    got = cloud.run_sync("admin")
    assert got["ok"] is False and "ZOOM_CLIENT_SECRET" in got["error"]
