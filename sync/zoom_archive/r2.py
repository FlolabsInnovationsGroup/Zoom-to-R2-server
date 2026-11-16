"""Cloudflare R2 (S3 API). Only head, get, list, put (plain and conditional) and multipart calls exist here.
There is no delete call, and the client refuses delete calls anyway (see guard)."""
import hashlib

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

PART_SIZE = 64 * 1024 * 1024  # files up to this size: one put_object; larger: multipart in these parts
TYPES = {"mp4": "video/mp4", "m4a": "audio/mp4", "vtt": "text/vtt", "txt": "text/plain",
         "json": "application/json", "jsonl": "application/x-ndjson", "md": "text/markdown",
         "sqlite": "application/vnd.sqlite3"}


def content_type(key: str) -> str:
    return TYPES.get(key.rsplit(".", 1)[-1], "application/octet-stream")


class SizeMismatch(RuntimeError):
    pass


class Conflict(RuntimeError):
    """A conditional write lost: the object changed since it was read."""


class AlreadyThere(RuntimeError):
    """A recording file already exists in R2 under this name: it is never overwritten."""


# This app never deletes anything in R2 and never changes the bucket's settings. Every R2 client it creates refuses
# these calls before they leave the process, whatever the R2 key would allow. (AbortMultipartUpload stays allowed:
# it only discards the parts of an upload that never finished; it cannot touch a stored file.)
FORBIDDEN_CALLS = ("DeleteObject", "DeleteObjects", "DeleteBucket", "PutBucketLifecycleConfiguration",
                   "DeleteBucketLifecycle", "PutBucketPolicy", "DeleteBucketPolicy", "PutBucketVersioning",
                   "PutObjectRetention", "PutObjectLockConfiguration")


def _refuse(event_name: str, **_):
    raise PermissionError(f"Refused: this app never deletes or reconfigures anything in R2 ({event_name.split('.')[-1]})")


def guard(client):
    for op in FORBIDDEN_CALLS:
        client.meta.events.register(f"before-call.s3.{op}", _refuse)
    return client


class R2:
    def __init__(self, cfg):
        self.bucket = cfg.r2_bucket
        self.s3 = guard(boto3.client(
            "s3", endpoint_url=cfg.r2_endpoint, region_name="auto", aws_access_key_id=cfg.r2_access_key_id,
            aws_secret_access_key=cfg.r2_secret_access_key,
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"},
                              retries={"max_attempts": 4, "mode": "standard"}, read_timeout=120)))

    def head(self, key: str) -> int | None:
        try:
            return int(self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise

    def sha256(self, key: str) -> str:
        body, h = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"], hashlib.sha256()
        for chunk in iter(lambda: body.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest()

    def list(self, prefix: str = "", max_keys: int | None = None) -> dict[str, int]:
        if max_keys:
            r = self.s3.list_objects_v2(Bucket=self.bucket, MaxKeys=max_keys, Prefix=prefix)
            return {o["Key"]: o["Size"] for o in r.get("Contents", [])}
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.update({o["Key"]: o["Size"] for o in page.get("Contents", [])})
        return out

    def write(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type(key))

    # --- used when the SQLite files live in R2 (serverless, see r2db.py) ---

    def etag(self, key: str) -> str | None:
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["ETag"]
        except ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise

    def download(self, key: str, path) -> str:
        """Object -> local file. Returns the ETag of what was downloaded."""
        r = self.s3.get_object(Bucket=self.bucket, Key=key)
        with open(path, "wb") as f:
            for chunk in iter(lambda: r["Body"].read(1 << 20), b""):
                f.write(chunk)
        return r["ETag"]

    def write_if(self, key: str, data: bytes, etag: str | None) -> str:
        """Write only if the object still has this ETag (None: only if it does not exist yet).
        Raises Conflict when someone else changed it first. Returns the new ETag."""
        cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            r = self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type(key), **cond)
        except ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
                raise Conflict(key) from None
            raise
        return r["ETag"]

    def keys_page(self, prefix: str, max_keys: int = 1000) -> "list[str]":
        """First max_keys keys under prefix, in key order."""
        r = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=max_keys)
        return [o["Key"] for o in r.get("Contents", [])]

    def read(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def upload_stream(self, key: str, chunks, expected: int, exact: bool = True) -> str:
        """Hash while streaming into R2. Returns sha256. Raises SizeMismatch unless exactly `expected`
        bytes arrived; nothing is completed then, and a multipart upload is aborted on any error."""
        h, n, buf, parts, upload_id = hashlib.sha256(), 0, bytearray(), [], None
        multipart = expected > PART_SIZE
        try:
            if multipart:
                upload_id = self.s3.create_multipart_upload(
                    Bucket=self.bucket, Key=key, ContentType=content_type(key))["UploadId"]
            for c in chunks:
                n += len(c)
                if exact and n > expected:
                    raise SizeMismatch(f"size mismatch: zoom {expected}, received more than that")
                h.update(c)
                buf += c
                while multipart and len(buf) >= PART_SIZE:
                    self._part(key, upload_id, parts, bytes(buf[:PART_SIZE]))
                    del buf[:PART_SIZE]
            if exact and n != expected:
                raise SizeMismatch(f"size mismatch: zoom {expected}, received {n}")
            if not multipart:  # create only: a file already in R2 is never replaced
                try:
                    self.s3.put_object(Bucket=self.bucket, Key=key, Body=bytes(buf), ContentType=content_type(key),
                                       IfNoneMatch="*")
                except ClientError as e:
                    if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
                        raise AlreadyThere(f"R2 already has {key}; it is never overwritten") from None
                    raise
            else:
                if buf:
                    self._part(key, upload_id, parts, bytes(buf))
                self.s3.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                                  MultipartUpload={"Parts": parts})
        except BaseException:
            if upload_id:
                try:
                    self.s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
                except Exception:  # noqa: BLE001 - the original error matters more
                    pass
            raise
        return h.hexdigest()

    def _part(self, key, upload_id, parts, data) -> None:
        n = len(parts) + 1
        r = self.s3.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=n, Body=data)
        parts.append({"PartNumber": n, "ETag": r["ETag"]})
