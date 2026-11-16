"""The archive only ever grows: nothing is deleted from R2, and a file in R2 is never overwritten.
Meetings deleted in Zoom stay in R2."""
import re
from pathlib import Path

import pytest
from conftest import admin_s3, recording_keys
from test_archive import three_users

from zoom_archive.config import load_config
from zoom_archive.r2 import FORBIDDEN_CALLS, R2

CODE = Path(__file__).resolve().parents[1] / "zoom_archive"


def test_the_r2_client_refuses_to_delete(env, bucket):
    admin_s3().put_object(Bucket=bucket, Key="pm-account/x/keep.mp4", Body=b"video")
    r2 = R2(load_config())
    with pytest.raises(PermissionError):
        r2.s3.delete_object(Bucket=bucket, Key="pm-account/x/keep.mp4")
    with pytest.raises(PermissionError):
        r2.s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "pm-account/x/keep.mp4"}]})
    with pytest.raises(PermissionError):
        r2.s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": []})
    assert admin_s3().get_object(Bucket=bucket, Key="pm-account/x/keep.mp4")["Body"].read() == b"video"


def test_no_delete_call_anywhere_in_the_code():
    pattern = re.compile(r"\.(delete_object|delete_objects|delete_bucket\w*|put_bucket_lifecycle\w*)\s*\(")
    hits = [f"{p.name}:{n}" for p in CODE.glob("*.py")
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert hits == [], hits
    assert {"DeleteObject", "DeleteObjects", "PutBucketLifecycleConfiguration"} <= set(FORBIDDEN_CALLS)


def test_meetings_deleted_in_zoom_stay_in_r2(env, fake_zoom, bucket, run_cli):
    three_users(fake_zoom)
    assert run_cli("--mode", "backfill")[0] == 0
    before = recording_keys(bucket)
    assert before
    fake_zoom.meetings.clear()  # every meeting deleted in Zoom
    assert run_cli("--mode", "sync")[0] == 0
    assert recording_keys(bucket) == before  # every file still there, same sizes


def test_a_file_already_in_r2_is_never_overwritten(env, fake_zoom, bucket, run_cli):
    m = three_users(fake_zoom)
    assert run_cli("--mode", "backfill", "--dry-run")[0] == 0
    code, out = run_cli("--mode", "backfill")
    keys = recording_keys(bucket)
    victim = next(iter(keys))
    admin_s3().put_object(Bucket=bucket, Key=victim, Body=b"something else already here")
    import sqlite3
    c = sqlite3.connect(env["MANIFEST_PATH"])
    c.execute("UPDATE files SET status = 'pending' WHERE r2_key = ?", (victim,))  # as if the copy was lost
    c.commit()
    c.close()
    run_cli("--mode", "backfill")
    assert admin_s3().get_object(Bucket=bucket, Key=victim)["Body"].read() == b"something else already here"
    c = sqlite3.connect(env["MANIFEST_PATH"])
    status, err = c.execute("SELECT status, last_error FROM files WHERE r2_key = ?", (victim,)).fetchone()
    assert status == "failed" and "never overwritten" in err
