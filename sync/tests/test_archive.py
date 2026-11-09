import json
import re
import threading
import time
from pathlib import Path

import jsonschema

from conftest import (BUDDY, CONTRACTS, JOHN, PM, get_json, get_text, list_keys, load_schema, manifest_query,
                      manifest_rows, recording_keys, three_users)
from zoom_archive import naming, outputs, runctl, search

SRC = Path(__file__).resolve().parents[1] / "zoom_archive"


def downloads(fake):
    return len(fake.requests_to("/rec/download/"))


def test_naming_reproduces_fixture_keys():
    accounts = naming.load_accounts(CONTRACTS / "access_groups.json")
    teams = naming.load_teams(Path(SRC).parent / "teams.yaml")
    for m in json.loads((CONTRACTS / "fixtures" / "all_meetings.json").read_text(encoding="utf-8")):
        team, _ = naming.match_team(teams, m["topic"], m["zoom_meeting_id"])
        account = naming.account_slug(m["host_email"], accounts)
        path = naming.meeting_path(account, team, m["zoom_meeting_id"], m["zoom_uuid"], m["topic"],
                                   m["start_time_utc"])
        assert (path.meeting_key, path.folder) == (m["meeting_key"], m["folder"])
        files = [{"id": f["zoom_file_id"], "file_type": f["file_type"], "recording_type": f["recording_type"],
                  "file_extension": f["name"].rsplit(".", 1)[-1], "recording_start": str(i)}
                 for i, f in enumerate(m["files"])]
        names = naming.assign_file_names(path, files)
        assert [names[f["zoom_file_id"]] for f in m["files"]] == [f["name"] for f in m["files"]]
    assert naming.file_kind("MP4", "shared_screen_with_speaker_view(CC)") == (
        "video-shared_screen_with_speaker_view-cc", "mp4")


def test_second_run_uploads_nothing(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    code, out = run_cli("--mode", "backfill")
    assert code == 0, out
    first = downloads(fake_zoom)
    assert first == len(fake_zoom.completed_files()) > 10
    rows = manifest_rows(env["MANIFEST_PATH"])
    assert rows and all(r["status"] == "verified" and r["sha256"] for r in rows)
    keys = recording_keys(bucket)
    assert set(keys) == {r["r2_key"] for r in rows}
    meeting = get_json(bucket, "pm-account/design/2026/2026-11-04_1400utc_design-team_1d3552d2/meeting.json")
    jsonschema.validate(meeting, load_schema())
    assert "restricted" not in meeting
    sys_keys = list_keys(bucket, "_system/").keys()  # plus a dated copy, so one bad run cannot erase the only backup
    assert "_system/manifest.sqlite" in sys_keys and any(re.fullmatch(r"_system/manifest-\d{4}-\d\d-\d\d\.sqlite", k) for k in sys_keys)

    code, out = run_cli("--mode", "backfill")
    assert code == 0 and downloads(fake_zoom) == first
    assert recording_keys(bucket) == keys


def test_size_mismatch_is_failed_never_verified(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    bad = "81234567890-f2"
    fake_zoom.download_faults[bad] = [{"short": 100}]
    code, out = run_cli("--mode", "backfill", "--meeting-id", "81234567890")
    assert code == 1
    row = next(r for r in manifest_rows(env["MANIFEST_PATH"]) if r["zoom_file_id"] == bad)
    assert row["status"] == "failed" and "size mismatch" in row["last_error"]
    assert row["r2_key"] not in recording_keys(bucket)
    assert not [k for k in list_keys(bucket) if k.endswith("/meeting.json")]  # incomplete meeting
    code, out = run_cli("--mode", "backfill", "--meeting-id", "81234567890")  # a rerun retries it
    assert code == 0 and row["r2_key"] in recording_keys(bucket)


def test_topic_change_after_partial_archive_keeps_one_folder(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    bad = "81234567890-f2"
    fake_zoom.download_faults[bad] = [{"short": 100}]
    run_cli("--mode", "backfill", "--meeting-id", "81234567890")
    for m in fake_zoom.meetings.values():
        if str(m["id"]) == "81234567890":
            m["topic"] = "Totally renamed topic"
    code, out = run_cli("--mode", "backfill", "--meeting-id", "81234567890")
    assert code == 0, out
    rows = [r for r in manifest_rows(env["MANIFEST_PATH"]) if str(r["meeting_id"]) == "81234567890"]
    assert len({r["r2_key"].rsplit("/", 1)[0] for r in rows}) == 1
    assert len({r["meeting_key"] for r in rows}) == 1


def test_hr_and_one_on_one_are_ordinary_teams_in_account_folders(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    assert run_cli("--mode", "backfill")[0] == 0
    keys = recording_keys(bucket)
    assert any(k.startswith("john-account/hr/2026/") for k in keys)
    assert any(k.startswith("pm-account/one-on-one/2026/") for k in keys)
    assert {k.split("/")[0] for k in list_keys(bucket)} <= {"pm-account", "john-account", "buddy-account", "_system"}
    hr = next(k for k in list_keys(bucket) if k.startswith("john-account/hr/") and k.endswith("/meeting.json"))
    jsonschema.validate(get_json(bucket, hr), load_schema())


def test_trash_inventory_then_recovered_meeting_archived_as_trash(env, fake_zoom, bucket, run_cli):
    m = three_users(fake_zoom)
    assert run_cli("--mode", "backfill", "--from", "2026-01-01")[0] == 0
    trash = manifest_query(env["MANIFEST_PATH"], "SELECT * FROM trash_items")
    assert len(trash) == 2 and not any(t["archived"] for t in trash)
    assert all(r["source"] == "active" for r in manifest_rows(env["MANIFEST_PATH"]))
    status = outputs.status_json(manifest_rows(env["MANIFEST_PATH"]), trash, {}, set())
    assert status["trash"]["purge_date"] == "2026-12-06"

    fake_zoom.recover(m["trash_pm"]["uuid"])
    assert run_cli("--mode", "backfill")[0] == 0
    rows = [r for r in manifest_rows(env["MANIFEST_PATH"]) if r["meeting_uuid"] == m["trash_pm"]["uuid"]]
    assert len(rows) == 2 and all(r["source"] == "trash" and r["status"] == "verified" for r in rows)
    flags = {t["meeting_uuid"]: t["archived"] for t in manifest_query(env["MANIFEST_PATH"], "SELECT * FROM trash_items")}
    assert flags[m["trash_pm"]["uuid"]] == 1 and flags[m["trash_buddy"]["uuid"]] == 0


def test_users_scope_error_4711_falls_back_to_emails(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    fake_zoom.users_scope_error = True
    code, out = run_cli("--mode", "backfill", "--user", PM)
    assert code == 0, out
    assert "4711" in out and "ZOOM_USER_EMAILS" in out
    assert any(PM.lower() in r["path"].lower() for r in fake_zoom.requests_to("/recordings"))
    assert recording_keys(bucket)


def test_429_retry_after_then_success(env, fake_zoom, bucket, run_cli, monkeypatch):
    three_users(fake_zoom)
    sleeps = []
    monkeypatch.setattr("zoom_archive.zoom.time.sleep", sleeps.append)
    fake_zoom.api_faults.append({"path": "/recordings", "status": 429, "headers": {"Retry-After": "7"}, "times": 1})
    code, out = run_cli("--mode", "backfill", "--limit", "2")
    assert code == 0, out
    assert 7.0 in sleeps and recording_keys(bucket)


def test_no_delete_calls_and_no_secrets_in_output(env, fake_zoom, bucket, run_cli, caplog):
    for src in SRC.glob("*.py"):
        text = src.read_text(encoding="utf-8")
        assert not re.search(r"delete_object|\.delete\(|\"(PUT|PATCH|DELETE)\"|requests\.(put|patch|delete)", text), src
    three_users(fake_zoom)
    caplog.set_level("DEBUG")
    fake_zoom.download_faults["81234567890-f3"] = [{"die_after": 10}]  # exercises an error path too
    code, out = run_cli("--mode", "backfill")
    assert code == 0, out
    assert {r["method"] for r in fake_zoom.requests} <= {"GET", "POST"}
    assert [r["path"] for r in fake_zoom.requests if r["method"] == "POST"] == ["/oauth/token"]
    everything = out + caplog.text + json.dumps(manifest_rows(env["MANIFEST_PATH"]))
    for secret in [*fake_zoom.tokens_issued, fake_zoom.client_secret, env["R2_SECRET_ACCESS_KEY"]]:
        assert secret not in everything
    assert "/rec/download/" not in everything and "access_token" not in everything


def test_status_json_has_per_account_progress(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    assert run_cli("--mode", "backfill")[0] == 0
    status = outputs.status_json(manifest_rows(env["MANIFEST_PATH"]), [], {}, {"pm", "john", "buddy"})
    pm = next(a for a in status["accounts"] if a["account"] == "pm")
    assert pm["meetings_complete"] == pm["meetings"] and pm["bytes_verified"] == pm["bytes"] > 0
    assert status["totals"]["verified"] == status["totals"]["files"]


def test_scan_lists_everything_as_pending_and_writes_nothing(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    code, out = run_cli("--mode", "scan")
    assert code == 0 and "scan done" in out, out
    rows = manifest_rows(env["MANIFEST_PATH"])
    assert len(rows) == len(fake_zoom.completed_files()) and all(r["status"] == "pending" for r in rows)
    assert downloads(fake_zoom) == 0 and list_keys(bucket) == {}
    assert [r["mode"] for r in manifest_query(env["MANIFEST_PATH"], "SELECT mode FROM runs")] == ["scan"]
    assert len(manifest_query(env["MANIFEST_PATH"], "SELECT * FROM trash_items")) == 2
    assert run_cli("--mode", "backfill")[0] == 0  # a later backfill downloads what the scan listed
    assert len(recording_keys(bucket)) == len(rows)


def test_stop_file_aborts_in_flight_download_and_leaves_files_pending(env, fake_zoom, bucket, run_cli, monkeypatch):
    three_users(fake_zoom)
    monkeypatch.setattr("zoom_archive.runctl.HEARTBEAT", 0.05)
    fake_zoom.download_faults["81234567890-f1"] = [{"stall_after": 10}]
    box = {}
    t = threading.Thread(target=lambda: box.update(r=run_cli("--mode", "backfill", "--meeting-id", "81234567890",
                                                             "--concurrency", "1")))
    t.start()
    assert fake_zoom.stalled.wait(15)
    assert runctl.running(env["MANIFEST_PATH"])
    Path(env["MANIFEST_PATH"]).with_name("sync.stop").write_text("1")
    time.sleep(0.5)  # the heartbeat thread notices the file
    fake_zoom.release.set()
    t.join(30)
    code, out = box["r"]
    assert code == 130, out
    rows = [r for r in manifest_rows(env["MANIFEST_PATH"]) if str(r["meeting_id"]) == "81234567890"]
    assert len(rows) == 6 and all(r["status"] == "pending" for r in rows)
    assert not recording_keys(bucket) and not [k for k in list_keys(bucket) if k.endswith("/meeting.json")]
    assert "_system/manifest.sqlite" in list_keys(bucket)
    assert runctl.running(env["MANIFEST_PATH"]) is None
    assert not Path(env["MANIFEST_PATH"]).with_name("sync.stop").exists()
    assert run_cli("--mode", "backfill", "--meeting-id", "81234567890")[0] == 0  # next run finishes them
    assert len(recording_keys(bucket)) == 6


def test_second_run_refused_while_one_is_alive(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    runctl.heartbeat(env["MANIFEST_PATH"], {"pid": 1, "mode": "backfill", "started_at": "2026-11-09T00:00:00Z"})
    for mode in ("backfill", "sync", "scan", "reindex"):
        code, out = run_cli("--mode", mode)
        assert code == 3 and "Another run is in progress" in out
    assert downloads(fake_zoom) == 0 and runctl.running(env["MANIFEST_PATH"])  # the other run's file is untouched


def test_transcript_is_indexed_and_reindex_rebuilds_it(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    assert run_cli("--mode", "backfill")[0] == 0
    db = search.default_path(env["MANIFEST_PATH"])
    hits = search.query(db, "hero section", lambda key: True)
    assert hits and hits[0]["hits"][0]["kind"] == "transcript" and hits[0]["hits"][0]["at"].startswith("00:00:")
    assert search.query(db, "hero", lambda key: key.startswith("pm/frontend/"))
    Path(db).unlink()
    assert search.query(db, "hero", lambda key: True) == []
    code, out = run_cli("--mode", "reindex")
    assert code == 0 and "reindex done" in out, out
    assert search.query(db, "hero", lambda key: True)


def test_unmapped_topic_is_filed_in_johns_folder(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)  # "Random brainstorm with partners" is hosted by Buddy and matches no team rule
    assert run_cli("--mode", "backfill")[0] == 0
    row = next(r for r in manifest_rows(env["MANIFEST_PATH"]) if r["topic"] == "Random brainstorm with partners")
    assert row["account"] == "john" and row["team"] == "misc" and row["unmapped"] == 1
    assert row["r2_key"].startswith("john-account/misc/2026/")
    n8n = next(r for r in manifest_rows(env["MANIFEST_PATH"]) if r["topic"] == "n8ntest")
    assert n8n["account"] == "pm"  # a topic a misc rule maps on purpose stays in its host's folder


def test_meeting_with_short_or_no_transcript_is_not_copied(env, fake_zoom, bucket, run_cli, monkeypatch):
    three_users(fake_zoom)
    monkeypatch.setenv("MIN_TRANSCRIPT_WORDS", "10")  # the fixture transcript has more than 10 words
    assert run_cli("--mode", "backfill")[0] == 0
    rows = manifest_rows(env["MANIFEST_PATH"])
    ops = [r for r in rows if r["topic"] == "Mohamed Operations Meeting"]  # audio only, no transcript
    assert ops and all(r["status"] == "skipped" and "0 words" in r["last_error"] for r in ops)
    assert all(r["r2_key"] not in recording_keys(bucket) for r in ops)
    design = [r for r in rows if r["topic"] == "Design Team" and r["source"] == "active"]
    assert design and all(r["status"] == "verified" for r in design)
    monkeypatch.setenv("MIN_TRANSCRIPT_WORDS", "100000")  # raising the bar never touches what is already copied
    assert run_cli("--mode", "backfill")[0] == 0
    assert all(r["status"] == "verified" for r in manifest_rows(env["MANIFEST_PATH"]) if r["topic"] == "Design Team"
               and r["source"] == "active")
    monkeypatch.setenv("MIN_TRANSCRIPT_WORDS", "0")  # lowering it copies the skipped ones on the next run
    assert run_cli("--mode", "backfill")[0] == 0
    assert all(r["status"] == "verified" for r in manifest_rows(env["MANIFEST_PATH"])
               if r["topic"] == "Mohamed Operations Meeting")


def test_summary_with_wrong_zoom_size_is_still_verified(env, fake_zoom, bucket, run_cli):
    m = three_users(fake_zoom)
    summary = next(f for f in m["humanoid"]["recording_files"] if f["recording_type"] == "summary")
    fake_zoom.content(summary["id"])  # served content is fixed at the true size ...
    summary["file_size"] += 6000      # ... but Zoom reports a bigger one, as it does for real summaries
    assert run_cli("--mode", "backfill", "--meeting-id", str(m["humanoid"]["id"]))[0] == 0
    row = next(r for r in manifest_rows(env["MANIFEST_PATH"]) if r["zoom_file_id"] == summary["id"])
    assert row["status"] == "verified" and row["r2_size"] == row["zoom_size"] - 6000


def test_unreadable_transcript_never_stops_the_scan(env, fake_zoom, bucket, run_cli, monkeypatch):
    m = three_users(fake_zoom)
    monkeypatch.setenv("MIN_TRANSCRIPT_WORDS", "10")
    t = next(f for f in m["frontend"]["recording_files"] if f["file_type"] == "TRANSCRIPT")
    fake_zoom.download_faults[t["id"]] = [{"status": 404}] * 5  # link and refreshed link both fail
    code, out = run_cli("--mode", "scan")
    assert code == 0 and "scan done" in out, out
    rows = [r for r in manifest_rows(env["MANIFEST_PATH"]) if r["topic"] == "Weekly FrontEnd"]
    assert rows and all(r["status"] == "pending" for r in rows)  # unknown word count: copied, not skipped
