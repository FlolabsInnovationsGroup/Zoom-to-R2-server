"""Shared fixtures: a fake Zoom (real local HTTP server) and moto's ThreadedMotoServer as R2.

Tests never load the repository .env: ZOOM_ARCHIVE_NO_DOTENV is set before anything imports the
package, and every variable the code reads is set explicitly per test.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

os.environ["ZOOM_ARCHIVE_NO_DOTENV"] = "1"
os.environ.pop("ENV_FILE", None)

import boto3  # noqa: E402
import pytest  # noqa: E402
import requests  # noqa: E402

SYNC_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SYNC_DIR.parent
CONTRACTS = REPO_ROOT / "contracts"
sys.path.insert(0, str(SYNC_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeZoom  # noqa: E402

MiB = 1024 * 1024
TEST_PART_SIZE = 5 * MiB          # moto, like S3/R2, needs >= 5 MiB for every part but the last
TODAY = date(2026, 11, 9)

JOHN = "john@flomadlabs.com"
PM = "PM@flomad.com"
BUDDY = "zoom.buddy@flolabsinnovations.com"

ENV_NAMES = ["ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_DOWNLOAD_AUTH", "ZOOM_USER_EMAILS",
             "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
             "AWS_REQUEST_CHECKSUM_CALCULATION", "AWS_RESPONSE_CHECKSUM_VALIDATION", "MANIFEST_PATH",
             "TEAMS_YAML", "SYNC_CONCURRENCY", "ZOOM_API_BASE", "ZOOM_OAUTH_URL",
             "R2_ENDPOINT_URL", "SEARCH_DB_PATH", "MIN_TRANSCRIPT_WORDS",
             "ADMIN_ORIGIN", "CODESPACE_NAME", "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"]


@pytest.fixture(scope="session")
def moto_endpoint():
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def fake_zoom():
    fake = FakeZoom().start()
    yield fake
    fake.stop()


@pytest.fixture
def bucket(moto_endpoint):
    name = f"test-archive-{uuid.uuid4().hex[:10]}"
    admin_s3().create_bucket(Bucket=name)
    return name


_ADMIN = {}


def admin_s3():
    """Test-side S3 client (not the code under test)."""
    if "c" not in _ADMIN:
        _ADMIN["c"] = boto3.client("s3", endpoint_url=_ADMIN["endpoint"], region_name="us-east-1",
                                   aws_access_key_id="testing", aws_secret_access_key="testing")
    return _ADMIN["c"]


@pytest.fixture(autouse=True)
def _admin_endpoint(moto_endpoint):
    _ADMIN["endpoint"] = moto_endpoint


@pytest.fixture
def env(monkeypatch, tmp_path, fake_zoom, moto_endpoint, bucket):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    values = {
        "ZOOM_ACCOUNT_ID": fake_zoom.account_id,
        "ZOOM_CLIENT_ID": fake_zoom.client_id,
        "ZOOM_CLIENT_SECRET": fake_zoom.client_secret,
        "ZOOM_DOWNLOAD_AUTH": "auto",
        "ZOOM_USER_EMAILS": f"{JOHN},{PM},{BUDDY}",
        "R2_ACCOUNT_ID": "test-account",
        "R2_ACCESS_KEY_ID": "testing",
        "R2_SECRET_ACCESS_KEY": "r2-secret-for-tests",
        "R2_BUCKET": bucket,
        "AWS_REQUEST_CHECKSUM_CALCULATION": "WHEN_REQUIRED",
        "AWS_RESPONSE_CHECKSUM_VALIDATION": "WHEN_REQUIRED",
        "MANIFEST_PATH": str(tmp_path / "state" / "manifest.sqlite"),
        "TEAMS_YAML": str(SYNC_DIR / "teams.yaml"),
        "SYNC_CONCURRENCY": "3",
        "ZOOM_API_BASE": fake_zoom.base + "/v2",
        "ZOOM_OAUTH_URL": fake_zoom.base + "/oauth/token",
        "R2_ENDPOINT_URL": moto_endpoint,
        "ZOOM_ARCHIVE_NO_DOTENV": "1",
        "MIN_TRANSCRIPT_WORDS": "0",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr("zoom_archive.zoom.BACKOFF", 0)
    monkeypatch.setattr("zoom_archive.r2.PART_SIZE", TEST_PART_SIZE)


@pytest.fixture
def run_cli(env, capsys):
    from zoom_archive import cli

    def _run(*args: str) -> tuple[int, str]:
        code = cli.main(["--from", "2026-06-01", "--to", "2026-11-09", *args])
        out = capsys.readouterr()
        return code, out.out + out.err

    return _run


def list_keys(bucket: str, prefix: str = "") -> dict[str, int]:
    out = {}
    for page in admin_s3().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []) or []:
            out[o["Key"]] = o["Size"]
    return out


def recording_keys(bucket: str) -> dict[str, int]:
    keys = {k: v for a in ("pm", "john", "buddy") for k, v in list_keys(bucket, f"{a}-account/").items()}
    return {k: v for k, v in keys.items() if not k.endswith("/meeting.json")}


def get_json(bucket: str, key: str):
    return json.loads(admin_s3().get_object(Bucket=bucket, Key=key)["Body"].read())


def get_text(bucket: str, key: str) -> str:
    return admin_s3().get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def moto_auth(endpoint: str, enforce: bool) -> None:
    """Turn moto's credential checking on (every unknown key is rejected) or off."""
    requests.post(endpoint + "/moto-api/reset-auth", data=b"0" if enforce else b"1000000000", timeout=10)


def load_schema() -> dict:
    return json.loads((CONTRACTS / "meeting.schema.json").read_text(encoding="utf-8"))


def manifest_rows(path: str) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM files ORDER BY zoom_file_id")]
    finally:
        conn.close()


def manifest_query(path: str, sql: str) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def three_users(fake: FakeZoom) -> dict:
    """The real account shape: 3 users (one inactive), Trash, several months, every file type."""
    fake.add_user(JOHN)
    fake.add_user(PM)
    fake.add_user(BUDDY, status="inactive")
    m = {}
    m["design"] = fake.add_meeting(PM, "Design Team", "2026-11-04T14:00:00Z", uuid="aB3/dE+fGhIjKlMnOpQr==",
                                   meeting_id=81234567890, duration=58, files=[
        {"file_type": "MP4", "recording_type": "shared_screen_with_gallery_view", "size": 11 * MiB + 123},
        {"file_type": "MP4", "recording_type": "shared_screen_with_speaker_view(CC)", "size": 2345},
        {"file_type": "M4A", "recording_type": "audio_only", "size": 2000},
        {"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "size": 1500},
        {"file_type": "CHAT", "recording_type": "chat_file", "size": 300},
        {"file_type": "TIMELINE", "recording_type": "timeline", "size": 400},
    ])
    m["frontend"] = fake.add_meeting(PM, "Weekly FrontEnd", "2026-11-05T09:30:00Z", files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 900},
        {"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "size": 1200},
    ])
    m["ops"] = fake.add_meeting(PM, "Mohamed Operations Meeting", "2026-11-03T08:00:00Z", files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 700},
    ])
    m["hr"] = fake.add_meeting(JOHN, "HR team meeting", "2026-10-20T10:00:00Z", files=[
        {"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "size": 800},
    ])
    m["one_to_one"] = fake.add_meeting(PM, "Ahsan / John 1:1", "2026-09-15T12:00:00Z", files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 600},
    ])
    m["humanoid"] = fake.add_meeting(JOHN, "Humanoid", "2026-11-08T21:30:00Z", duration=150, files=[
        {"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "size": 5000},
        {"file_type": "SUMMARY", "recording_type": "summary", "size": 640},
        {"file_type": "SUMMARY", "recording_type": "summary_next_steps", "size": 210},
    ])
    m["arabic"] = fake.add_meeting(BUDDY, "اجتماع الفريق", "2026-07-14T18:00:00Z", uuid="/xYz//AbCdEfGhIjKlMn==",
                                   meeting_id=85512345678, duration=33, files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 3000},
        {"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "size": 1000},
    ])
    m["unmapped"] = fake.add_meeting(BUDDY, "Random brainstorm with partners", "2026-10-02T16:00:00Z", files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 1100},
    ])
    m["n8n"] = fake.add_meeting(PM, "n8ntest", "2026-08-03T10:00:00Z", files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 500},
    ])
    m["trash_pm"] = fake.add_meeting(PM, "Design Team", "2026-07-21T14:00:00Z", trash=True, files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 4000},
        {"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "size": 900},
    ])
    m["trash_buddy"] = fake.add_meeting(BUDDY, "Space Team", "2026-04-14T09:00:00Z", trash=True, files=[
        {"file_type": "M4A", "recording_type": "audio_only", "size": 2500},
    ])
    return m
