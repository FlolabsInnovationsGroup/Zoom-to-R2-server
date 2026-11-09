"""Cloudflare R2 (S3 API). Only head, get (to hash), list, put and multipart calls exist here."""
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


class R2:
    def __init__(self, cfg):
        self.bucket = cfg.r2_bucket
        self.s3 = boto3.client(
            "s3", endpoint_url=cfg.r2_endpoint, region_name="auto", aws_access_key_id=cfg.r2_access_key_id,
            aws_secret_access_key=cfg.r2_secret_access_key,
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"},
                              retries={"max_attempts": 4, "mode": "standard"}, read_timeout=120))

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
            if not multipart:
                self.write(key, bytes(buf))
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
