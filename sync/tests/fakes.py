"""A stateful fake Zoom running as a real local threaded HTTP server.

It follows the live API shapes (PRD Appendix B, docs/ZOOM_FINDINGS.md):
- POST /oauth/token (account_credentials, basic auth) -> access_token, expires_in, scope
- GET /v2/users?status=..&page_size=..&next_page_token=..
- GET /v2/users/{userId or email}/recordings?from&to | ?trash=true&trash_type=meeting_recordings
- GET /v2/meetings/{id or (double-)encoded uuid}/recordings
- GET /rec/download/{file_id} with Authorization: Bearer or ?access_token=
Pages are small (page_size is ignored in favour of `page_size_override`) so next_page_token is
always exercised. Every request is logged; non-GET requests other than the token are refused.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

VTT = (Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "transcript.vtt").read_bytes()

DEFAULT_SCOPES = ("user:read:list_users:admin cloud_recording:read:list_user_recordings:admin "
                  "cloud_recording:read:list_recording_files:admin cloud_recording:read:recording:admin")

DEFAULT_EXT = {"MP4": "MP4", "M4A": "M4A", "TRANSCRIPT": "VTT", "CC": "VTT", "CHAT": "TXT",
               "TIMELINE": "JSON", "SUMMARY": "JSON"}


def content_for(file_id: str, size: int) -> bytes:
    seed = hashlib.sha256(file_id.encode()).digest()
    reps = size // len(seed) + 1
    return (seed * reps)[:size]


class FakeZoom:
    def __init__(self, account_id="acct-test", client_id="cid-test", client_secret="csecret-test-123"):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = DEFAULT_SCOPES
        self.expires_in = 3599
        self.users: list[dict] = []
        self.meetings: dict[str, dict] = {}
        self.download_auth = {"header"}           # methods Zoom accepts: header and/or query
        self.page_size_override = 2
        self.users_scope_error = False
        self.requests: list[dict] = []
        self.api_faults: list[dict] = []          # {"path": substring, "status", "headers", "body"}
        self.download_faults: dict[str, list[dict]] = {}
        self.webhook_bodies: list[bytes] = []
        self.tokens_issued: list[str] = []
        self.valid_tokens: set[str] = set()
        self.stalled = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self._ids = itertools.count(1)
        self._content: dict[str, bytes] = {}
        self.server: ThreadingHTTPServer | None = None

    # --- setup -------------------------------------------------------------------------------

    def add_user(self, email: str, status: str = "active") -> dict:
        u = {"id": f"u{len(self.users) + 1}x{hashlib.md5(email.encode()).hexdigest()[:6]}", "email": email,
             "status": status, "type": 2}
        self.users.append(u)
        return u

    def user(self, email: str) -> dict:
        return next(u for u in self.users if u["email"].lower() == email.lower())

    def add_meeting(self, host_email: str, topic: str, start_time: str, files: list[dict], *,
                    uuid: str | None = None, meeting_id: int | None = None, trash: bool = False,
                    duration: int = 30) -> dict:
        n = next(self._ids)
        meeting_id = meeting_id or 80000000000 + n
        uuid = uuid or base64.b64encode(hashlib.sha256(f"m{n}{topic}".encode()).digest()[:16]).decode()
        host = self.user(host_email)
        rec_files = []
        for i, spec in enumerate(files, start=1):
            ft = spec.get("file_type", "MP4")
            rec_files.append({
                "id": spec.get("id") or f"{meeting_id}-f{i}",
                "meeting_id": uuid,
                "recording_start": spec.get("recording_start") or start_time,
                "recording_end": spec.get("recording_end") or start_time,
                "file_type": ft,
                "file_extension": spec.get("file_extension", DEFAULT_EXT.get(ft, "BIN")),
                "file_size": len(VTT) if ft == "TRANSCRIPT" else spec.get("size", 1000),  # valid VTT is served
                "recording_type": spec.get("recording_type", "shared_screen_with_speaker_view"),
                "status": spec.get("status", "completed"),
            })
        m = {"uuid": uuid, "id": meeting_id, "account_id": self.account_id, "host_id": host["id"],
             "host_email": host["email"], "topic": topic, "type": 8, "start_time": start_time,
             "timezone": "Africa/Cairo", "duration": duration, "recording_count": len(rec_files),
             "share_url": f"https://zoom.example/share/{meeting_id}", "recording_files": rec_files,
             "_trash": trash, "_deleted_time": "2026-11-06T10:00:00Z" if trash else None}
        self.meetings[uuid] = m
        return m

    def recover(self, uuid: str) -> None:
        self.meetings[uuid]["_trash"] = False

    def set_file(self, file_id: str, **changes) -> None:
        for m in self.meetings.values():
            for f in m["recording_files"]:
                if f["id"] == file_id:
                    f.update(changes)
                    self._content.pop(file_id, None)
                    return
        raise KeyError(file_id)

    def completed_files(self) -> list[dict]:
        return [f for m in self.meetings.values() if not m["_trash"]
                for f in m["recording_files"] if f["status"] == "completed"]

    def content(self, file_id: str) -> bytes:
        f = next(f for m in self.meetings.values() for f in m["recording_files"] if f["id"] == file_id)
        if file_id not in self._content:
            self._content[file_id] = (VTT if f["file_type"] == "TRANSCRIPT" and f["file_size"] == len(VTT)
                                      else content_for(file_id, f["file_size"]))
        return self._content[file_id]

    def revoke_tokens(self) -> None:
        with self.lock:
            self.valid_tokens.clear()

    def requests_to(self, fragment: str, method: str = "GET") -> list[dict]:
        return [r for r in self.requests if fragment in r["path"] and r["method"] == method]

    # --- server ------------------------------------------------------------------------------

    def start(self) -> "FakeZoom":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def do_GET(self):
                fake._handle(self, "GET")

            def do_POST(self):
                fake._handle(self, "POST")

            def do_PUT(self):
                fake._handle(self, "PUT")

            def do_PATCH(self):
                fake._handle(self, "PATCH")

            def do_DELETE(self):
                fake._handle(self, "DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.release.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    # --- request handling --------------------------------------------------------------------

    def _json(self, h, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _token_of(self, h, query: dict) -> str | None:
        auth = h.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return (query.get("access_token") or [None])[0]

    def _handle(self, h, method: str) -> None:
        parts = urlsplit(h.path)
        raw_path = parts.path
        query = parse_qs(parts.query)
        auth = h.headers.get("Authorization", "")
        with self.lock:
            self.requests.append({"method": method, "path": raw_path, "query": {k: v[0] for k, v in query.items()},
                                  "auth": "bearer" if auth.startswith("Bearer ") else ("basic" if auth else None),
                                  "t": time.monotonic()})
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""

        if method == "POST" and raw_path == "/hook":
            self.webhook_bodies.append(body)
            return self._json(h, 200, {"ok": True})
        if method == "POST" and raw_path == "/oauth/token":
            return self._token(h, query)
        if method != "GET":
            return self._json(h, 405, {"code": 405, "message": "fake zoom is read-only"})

        if raw_path.startswith("/rec/download/"):
            return self._download(h, unquote(raw_path[len("/rec/download/"):]), query)

        token = self._token_of(h, query)
        if not auth.startswith("Bearer ") or token not in self.valid_tokens:
            return self._json(h, 401, {"code": 124, "message": "Invalid access token."})
        with self.lock:
            for fault in self.api_faults:
                if fault["path"] in raw_path and fault.get("times", 1) > 0:
                    fault["times"] = fault.get("times", 1) - 1
                    return self._json(h, fault["status"], fault.get("body", {"code": fault["status"]}),
                                      fault.get("headers"))
        if raw_path == "/v2/users":
            return self._users(h, query)
        if raw_path.startswith("/v2/users/") and raw_path.endswith("/recordings"):
            return self._user_recordings(h, unquote(raw_path[len("/v2/users/"):-len("/recordings")]), query)
        if raw_path.startswith("/v2/meetings/") and raw_path.endswith("/recordings"):
            return self._meeting_recordings(h, raw_path[len("/v2/meetings/"):-len("/recordings")])
        return self._json(h, 404, {"code": 404, "message": "not found"})

    def _token(self, h, query: dict) -> None:
        auth = h.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if auth != expected or query.get("grant_type") != ["account_credentials"] \
                or query.get("account_id") != [self.account_id]:
            return self._json(h, 400, {"reason": "Invalid client_id or client_secret", "error": "invalid_client"})
        with self.lock:
            token = f"fake-zoom-token-{len(self.tokens_issued) + 1}-{hashlib.md5(str(time.time()).encode()).hexdigest()}"
            self.tokens_issued.append(token)
            self.valid_tokens.add(token)
        return self._json(h, 200, {"access_token": token, "token_type": "bearer", "expires_in": self.expires_in,
                                   "scope": self.scopes, "api_url": "https://api.zoom.us"})

    def _page(self, items: list, query: dict) -> tuple[list, str]:
        start = int(query.get("next_page_token", ["0"])[0] or 0)
        size = self.page_size_override
        page = items[start:start + size]
        nxt = str(start + size) if start + size < len(items) else ""
        return page, nxt

    def _users(self, h, query: dict) -> None:
        if self.users_scope_error:
            return self._json(h, 400, {"code": 4711, "message":
                                       "Invalid access token, does not contain scopes:[user:read:list_users:admin]."})
        status = query.get("status", ["active"])[0]
        users = [u for u in self.users if u["status"] == status]
        page, nxt = self._page(users, query)
        return self._json(h, 200, {"page_size": 300, "total_records": len(users), "next_page_token": nxt,
                                   "users": page})

    def _public_meeting(self, m: dict, with_links: bool = True) -> dict:
        out = {k: v for k, v in m.items() if not k.startswith("_")}
        files = []
        for f in m["recording_files"]:
            f = dict(f)
            if with_links and f["status"] == "completed":
                f["download_url"] = f"{self.base}/rec/download/{f['id']}"
            files.append(f)
        out["recording_files"] = files
        out["total_size"] = sum(f["file_size"] for f in m["recording_files"])
        return out

    def _user_recordings(self, h, user_ref: str, query: dict) -> None:
        user = next((u for u in self.users if user_ref == u["id"] or user_ref.lower() == u["email"].lower()), None)  # Zoom ignores email case
        if user is None:
            return self._json(h, 404, {"code": 1001, "message": "User does not exist."})
        mine = [m for m in self.meetings.values() if m["host_id"] == user["id"]]
        if query.get("trash") == ["true"]:
            # ZOOM_FINDINGS Z6: one entry per meeting, empty file_type, no status, no id, no link.
            items = []
            for m in mine:
                if m["_trash"]:
                    items.append({"uuid": m["uuid"], "id": m["id"], "host_id": m["host_id"], "topic": m["topic"],
                                  "start_time": m["start_time"], "duration": m["duration"],
                                  "total_size": sum(f["file_size"] for f in m["recording_files"]),
                                  "recording_count": len(m["recording_files"]),
                                  "recording_files": [{"file_type": "", "deleted_time": m["_deleted_time"]}]})
        else:
            if "from" not in query or "to" not in query:
                return self._json(h, 400, {"code": 300, "message": "from and to are required by this fake"})
            frm, to = date.fromisoformat(query["from"][0]), date.fromisoformat(query["to"][0])
            if (to - frm).days > 31:
                return self._json(h, 400, {"code": 300, "message": "range over one month"})
            items = [self._public_meeting(m) for m in mine if not m["_trash"]
                     and frm <= datetime.fromisoformat(m["start_time"].replace("Z", "+00:00")).date() <= to]
            items.sort(key=lambda m: m["start_time"], reverse=True)
        page, nxt = self._page(items, query)
        return self._json(h, 200, {"page_size": 300, "total_records": len(items), "next_page_token": nxt,
                                   "meetings": page})

    def _meeting_recordings(self, h, segment: str) -> None:
        once = unquote(segment)
        if "%" in once:
            ref = unquote(once)
        else:
            ref = once
            if ref.startswith("/") or "//" in ref:
                # Real Zoom misreads a single-encoded UUID like this.
                return self._json(h, 404, {"code": 3301, "message": "This recording does not exist."})
        m = self.meetings.get(ref) or next((x for x in self.meetings.values() if str(x["id"]) == ref), None)
        if m is None or m["_trash"]:
            return self._json(h, 404, {"code": 3301, "message": "This recording does not exist."})
        return self._json(h, 200, self._public_meeting(m))

    def _download(self, h, file_id: str, query: dict) -> None:
        token = None
        auth = h.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and "header" in self.download_auth:
            token = auth[7:]
        elif "access_token" in query and "query" in self.download_auth:
            token = query["access_token"][0]
        if token is None or token not in self.valid_tokens:
            return self._json(h, 401, {"code": 124, "message": "Invalid access token."})
        with self.lock:
            faults = self.download_faults.get(file_id) or []
            fault = faults.pop(0) if faults else None
        if fault and "status" in fault:
            return self._json(h, fault["status"], {"code": fault["status"], "message": "fault"})
        data = self.content(file_id)
        send = data
        length = len(data)
        if fault and "short" in fault:                      # honest Content-Length, fewer bytes than Zoom says
            send = data[: fault["short"]]
            length = len(send)
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Length", str(length))
        h.end_headers()
        limit = None
        if fault and "die_after" in fault:
            limit = fault["die_after"]
        if fault and "stall_after" in fault:
            limit = fault["stall_after"]
        sent = 0
        chunk = 64 * 1024
        try:
            while sent < len(send):
                piece = send[sent: sent + chunk]
                if limit is not None and sent + len(piece) > limit:
                    piece = send[sent:limit]
                h.wfile.write(piece)
                sent += len(piece)
                if limit is not None and sent >= limit:
                    h.wfile.flush()
                    if "stall_after" in fault:
                        self.stalled.set()
                        self.release.wait(60)
                    h.close_connection = True
                    h.connection.shutdown(2)
                    return
        except (ConnectionError, OSError):
            return
