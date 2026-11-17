"""Admin page + MCP server in one Starlette app. Start: python -m zoom_archive.web (Caddy gives it HTTPS).

  /admin, /admin/login, /admin/logout   password login -> signed session cookie; run control, status, API keys, log
  /admin/recordings                     every meeting in the manifest with its archive status
  /mcp/{key}                            MCP over streamable HTTP, key in the URL
  /mcp                                  same, key in "Authorization: Bearer <key>"

The key is in the URL, so request paths are never logged: uvicorn's access log is off (see main()) and
nothing in this package logs a path.
"""
import asyncio
import hmac
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from secrets import token_urlsafe

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware import Middleware
from starlette.routing import Route
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import admin_page, r2db, runctl, search
from .access import (ACCESS, GROUPS, ReadOnlyR2, active_key, connect, init_keys_db, key_state, new_api_key, now_iso,
                     parse_groups, read_manifest, sha256_hex)
from .config import ConfigError, load_web_config
from .r2 import R2
from .mcp_server import CALLER, Archive, build_mcp
from .outputs import status_json

COOKIE = "fl_session"
NEW_KEY_COOKIE = "fl_new_key"  # signed, 2 minutes, read once: carries a just-created key across the redirect
SESSION_SECONDS = 12 * 3600
FAIL_DELAY = 1.0  # seconds a wrong password waits before the answer
MAX_FAILS, LOCKOUT = 5, 15 * 60  # this many wrong passwords in a row lock the login for LOCKOUT seconds (all clients)
LOCAL_HOSTS = ("localhost", "127.0.0.1")


# ---- sync status, live from the local manifest ---------------------------------------------------

def live_status(manifest_path: str):
    """The same dict sync writes as status, built from the manifest (opened read-only). None if no data yet."""
    got = read_manifest(manifest_path, "SELECT * FROM files", "SELECT * FROM trash_items",
                        "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    if not got:
        return None
    files, trash, run = got
    last = {k: run[0][k] for k in ("mode", "started_at", "finished_at", "files_done", "files_failed", "files_skipped",
                                   "bytes", "exit_code")} if run else None
    known = {v for v in ACCESS["accounts"].values()}
    status = status_json(files, trash, last, known)
    t = status["totals"]
    t["videos"] = sum(f["file_type"] == "MP4" and f["status"] != "skipped" for f in files)
    t["videos_done"] = sum(f["file_type"] == "MP4" and f["status"] == "verified" for f in files)
    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t["speed_bps"] = sum(int(f["zoom_size"] or 0) for f in files  # bytes copied in the last 10 minutes / 600 s
                         if f["status"] == "verified" and (f["updated_at"] or "") >= since) / 600
    for a in status["accounts"]:  # per-account archived share; newer status_json already has these
        if "meetings_complete" not in a:
            mine = [m for m in meetings_of(files) if m["account"] == a["account"]]
            a["meetings_complete"] = sum(m["state"] == "archived" for m in mine)
            a["bytes_verified"] = sum(int(f["zoom_size"] or 0) for f in files
                                      if f["account"] == a["account"] and f["status"] == "verified")
    return status


def meetings_of(files: list[dict]) -> list[dict]:
    """Manifest file rows -> one dict per meeting, newest first, with its archive state."""
    by: dict[str, list[dict]] = {}
    for f in files:
        by.setdefault(f["meeting_uuid"], []).append(f)
    out = []
    for rows in by.values():
        ok = sum(r["status"] == "verified" for r in rows)
        state = ("skipped" if all(r["status"] == "skipped" for r in rows) else
                 "archived" if ok == len(rows) else "failed" if any(r["status"] == "failed" for r in rows)
                 else "partial" if ok else "pending")
        first = rows[0]
        out.append({"start": first["start_time_utc"] or "", "account": first["account"], "team": first["team"],
                    "topic": first["topic"] or "", "files": len(rows), "verified": ok, "state": state,
                    "bytes": sum(int(r["zoom_size"] or 0) for r in rows)})
    return sorted(out, key=lambda m: m["start"], reverse=True)


PAGE_SIZE = 200


def recordings_data(manifest_path: str, account: str, status: str, page: int, q: str = "") -> dict:
    got = read_manifest(manifest_path, "SELECT meeting_uuid, topic, account, team, start_time_utc, status, zoom_size "
                        "FROM files")
    meetings = meetings_of(got[0]) if got else []
    totals = {"meetings": len(meetings), "bytes": sum(m["bytes"] for m in meetings)}
    for st in ("archived", "pending", "failed", "skipped"):
        totals[st] = sum(m["state"] == st or (st == "pending" and m["state"] == "partial") for m in meetings)
    q = q.strip().casefold()
    shown = [m for m in meetings if (not account or m["account"] == account) and q in m["topic"].casefold()
             and (not status or m["state"] == status or (status == "pending" and m["state"] == "partial"))]
    pages = max(1, -(-len(shown) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    return {"meetings": shown[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], "shown": len(shown), "totals": totals,
            "page": page, "pages": pages, "first": (page - 1) * PAGE_SIZE + 1}


# ---- the app --------------------------------------------------------------------------------------

def origin_of(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def create_app(cfg: dict | None = None) -> Starlette:
    cfg = cfg or load_web_config()  # raises ConfigError (no ADMIN_PASSWORD etc.) -> main() prints it and exits
    db_path = cfg["keys_db_path"]
    manifest_path = cfg["manifest_path"]
    serverless = r2db.enabled()  # Vercel: the SQLite files live in R2 (see r2db.py)
    rw = R2(cfg["r2_rw"]) if serverless else None
    if serverless:
        if r2db.pull(rw, "keys") is None:  # first start: create the (empty) key list in R2
            r2db.edit(rw, "keys", lambda p: None, create=init_keys_db)
    else:
        init_keys_db(db_path)
    archive = Archive(ReadOnlyR2(cfg["r2"]), db_path, manifest_path,
                      cfg.get("search_db_path") or search.default_path(manifest_path),
                      log=(lambda *a: r2db.log_access(rw, *a)) if serverless else None)
    mcp = build_mcp(archive)
    mcp.streamable_http_app()  # creates mcp.session_manager
    signer = URLSafeTimedSerializer(cfg["secret_key"], salt="admin-session")
    password = cfg["admin_password"].encode()
    # Keyed with the secret, so a copied cookie does not let anyone crack the password offline.
    session_id = hmac.new(cfg["secret_key"].encode(), cfg["admin_password"].encode(), "sha256").hexdigest()[:16]
    auth = {"fails": 0, "locked_until": 0.0, "valid_after": 0}  # valid_after: logout ends every older session

    def signed_in(request: Request) -> bool:
        try:
            sid, ts = signer.loads(request.cookies.get(COOKIE, ""), max_age=SESSION_SECONDS, return_timestamp=True)
        except BadSignature:
            return False
        return sid == session_id and ts.timestamp() > auth["valid_after"]

    def same_origin(request: Request) -> bool:
        got = request.headers.get("origin")
        return bool(got) and got in (origin_of(request), *(cfg.get("origins") or [cfg.get("origin")]))

    def html(body: str, nonce: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(body, status, {"Cache-Control": "no-store", "Content-Security-Policy": admin_page.csp(nonce)})

    flash = URLSafeTimedSerializer(cfg["secret_key"], salt="new-key")

    def admin_view(request: Request, status: int = 200, new_key: str = "", error: str = "") -> HTMLResponse:
        c = connect(db_path)
        try:
            rows = c.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
            calls = [] if serverless else [dict(r) for r in c.execute(
                "SELECT l.at, l.tool, l.meeting_key, l.result, k.name FROM access_log l "
                "LEFT JOIN api_keys k ON k.id = l.key_id ORDER BY l.id DESC LIMIT 50").fetchall()]
        finally:
            c.close()
        keys = [{**dict(r), "state": key_state(r), "groups": parse_groups(r["access_groups"])} for r in rows]
        names_all = {k["id"]: k["name"] for k in keys}
        keys = [k for k in keys if k["state"] != "revoked"]  # revoked keys are history, not shown
        cloud = None
        if serverless:
            entries, last_used = r2db.recent_access(rw)
            calls = [{**e, "name": names_all.get(e.get("key_id"))} for e in entries]
            for k in keys:
                k["last_used_at"] = last_used.get(k["id"]) or k.get("last_used_at")
            cloud = r2db.lock_state(rw)
        nonce = token_urlsafe(12)
        page = admin_page.render_admin(status=live_status(manifest_path), keys=keys, log=calls,
                                       run=None if serverless else runctl.running(manifest_path),
                                       run_log=[] if serverless else runctl.log_tail(manifest_path, 15),
                                       cloud=cloud, groups=GROUPS, origin=cfg.get("origin") or origin_of(request),
                                       nonce=nonce, new_key=new_key, error=error)
        return html(page, nonce, status)

    async def root(request):
        return RedirectResponse("/admin", 303)

    async def recordings(request: Request):
        if not signed_in(request):
            return RedirectResponse("/admin/login", 303)
        q = request.query_params
        account = q.get("account", "") if q.get("account", "") in ACCESS["accounts"].values() else ""
        status = q.get("status", "") if q.get("status", "") in ("archived", "pending", "failed", "skipped") else ""
        page = int(q["page"]) if q.get("page", "").isdigit() else 1
        text = q.get("q", "")[:100]
        nonce = token_urlsafe(12)
        data = await asyncio.to_thread(recordings_data, manifest_path, account, status, page, text)
        return html(admin_page.render_recordings(**data, account=account, status=status, q=text, nonce=nonce), nonce)

    async def login(request: Request):
        nonce = token_urlsafe(12)
        if request.method == "GET":
            return html(admin_page.render_login(nonce), nonce)
        if not same_origin(request):
            return PlainTextResponse("Forbidden", 403)
        if time.time() < auth["locked_until"]:
            return html(admin_page.render_login(nonce, "Too many wrong passwords. Try again in 15 minutes."), nonce, 429)
        given = str((await request.form()).get("password") or "").encode()
        if not hmac.compare_digest(given, password):
            auth["fails"] += 1
            if auth["fails"] >= MAX_FAILS:
                auth["fails"], auth["locked_until"] = 0, time.time() + LOCKOUT
            await asyncio.sleep(FAIL_DELAY)
            return html(admin_page.render_login(nonce, "Wrong password."), nonce, 401)
        auth["fails"] = 0
        resp = RedirectResponse("/admin", 303)
        resp.set_cookie(COOKIE, signer.dumps(session_id), max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                        secure=request.url.hostname not in LOCAL_HOSTS)
        return resp

    async def logout(request: Request):
        if not same_origin(request):
            return PlainTextResponse("Forbidden", 403)
        auth["valid_after"] = int(time.time())  # a copied cookie stops working too
        resp = RedirectResponse("/admin/login", 303)
        resp.delete_cookie(COOKIE)
        return resp

    async def admin(request: Request):
        if not signed_in(request):
            return RedirectResponse("/admin/login", 303) if request.method == "GET" else PlainTextResponse("Forbidden", 403)
        if request.method == "GET":
            shown = ""
            if request.cookies.get(NEW_KEY_COOKIE):  # a key created just now: shown once, then forgotten
                try:
                    shown = flash.loads(request.cookies[NEW_KEY_COOKIE], max_age=120)
                except BadSignature:
                    shown = ""
            resp = admin_view(request, new_key=shown)
            if request.cookies.get(NEW_KEY_COOKIE):
                resp.delete_cookie(NEW_KEY_COOKIE, path="/admin")
            return resp
        if not same_origin(request):
            return PlainTextResponse("Forbidden", 403)
        form = await request.form()
        action = form.get("action")
        if action == "sync_now" and serverless:
            from . import cloud as cloud_sync
            got = await asyncio.to_thread(cloud_sync.run_sync, "admin")
            if got.get("busy"):
                return admin_view(request, 409, error="A sync is already running. Wait until it has finished.")
            return RedirectResponse("/admin", 303)
        if action in ("start_backfill", "start_scan") and not serverless:
            mode = "backfill" if action == "start_backfill" else "scan"
            if not await asyncio.to_thread(runctl.start, manifest_path, mode):
                return admin_view(request, 409, error="A run is already going. Stop it, or wait until it has finished.")
            return RedirectResponse("/admin", 303)
        if action == "stop" and not serverless:
            runctl.request_stop(manifest_path)
            return RedirectResponse("/admin", 303)
        key_id = str(form.get("id") or "")
        groups = [g for g in form.getlist("groups") if g in GROUPS]
        new_key, row = "", None
        if action == "create":
            name, day = str(form.get("name") or "").strip()[:100], str(form.get("expires") or "")
            try:
                if day:
                    date.fromisoformat(day)
            except ValueError:
                day = "bad"
            if not name or not groups or day == "bad":
                return admin_view(request, 400, error="Give the key a name and tick at least one group.")
            new_key = new_api_key()
            row = (str(uuid.uuid4()), name, new_key[:12], sha256_hex(new_key), json.dumps(groups), "admin",
                   now_iso(), f"{day}T23:59:59Z" if day else None)

        def apply(path: str) -> None:
            c = connect(path)
            try:
                with c:
                    if action == "create":
                        c.execute("INSERT INTO api_keys (id, name, key_prefix, key_hash, access_groups, created_by, "
                                  "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)", row)
                    elif action == "groups" and groups:
                        c.execute("UPDATE api_keys SET access_groups = ? WHERE id = ?", (json.dumps(groups), key_id))
                    elif action == "revoke":
                        c.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                  (now_iso(), key_id))
            finally:
                c.close()

        if action in ("create", "groups", "revoke"):
            if serverless:  # saved to R2 only if nobody else changed the key list meanwhile (retried otherwise)
                await asyncio.to_thread(r2db.edit, rw, "keys", apply)
            else:
                apply(db_path)
        resp = RedirectResponse("/admin", 303)  # a refresh after this never re-sends the form (no duplicate keys)
        if action == "create":
            resp.set_cookie(NEW_KEY_COOKIE, flash.dumps(new_key), max_age=120, httponly=True, samesite="strict",
                            secure=request.url.hostname not in LOCAL_HOSTS, path="/admin")
        return resp

    async def health(request: Request):
        """Public: is the app up, and which commit is deployed (Vercel sets VERCEL_GIT_COMMIT_SHA)."""
        import os
        return JSONResponse({"ok": True, "commit": (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "local")[:7]})

    async def cron_sync(request: Request):
        """Vercel cron (every 2 hours): Authorization: Bearer <CRON_SECRET>."""
        secret = cfg.get("cron_secret") or ""
        given = request.headers.get("authorization", "")
        if not serverless or not secret or not hmac.compare_digest(given.encode(), f"Bearer {secret}".encode()):
            return PlainTextResponse("Unauthorized", 401)
        from . import cloud as cloud_sync
        got = await asyncio.to_thread(cloud_sync.run_sync, "cron")
        return JSONResponse(got, 409 if got.get("busy") else 200)

    class McpApp:
        """ASGI app: authenticate (key row re-checked on every request), then hand over to the MCP SDK."""

        async def __call__(self, scope, receive, send):
            key = scope["path_params"].get("key")
            if key is None:
                auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
                key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            caller = await asyncio.to_thread(active_key, db_path, key)
            if not caller:
                resp = JSONResponse({"error": "invalid_token", "error_description": "API key revoked, expired or unknown"},
                                    401, {"WWW-Authenticate": 'Bearer error="invalid_token"'})
                return await resp(scope, receive, send)
            token = CALLER.set(caller)
            try:
                if serverless:  # stateless: a fresh manager per request, so nothing depends on app startup events
                    sm = StreamableHTTPSessionManager(app=mcp._mcp_server, json_response=True, stateless=True,
                                                      security_settings=mcp.settings.transport_security)
                    async with sm.run():
                        await sm.handle_request(scope, receive, send)
                else:
                    await mcp.session_manager.handle_request(scope, receive, send)
            finally:
                CALLER.reset(token)

    @asynccontextmanager
    async def lifespan(app):
        if serverless:
            yield
            return
        async with mcp.session_manager.run():
            yield

    class Refresh:
        """Serverless: before each request, make sure the local copies of the R2 files are current
        (one cheap check per file at most every 30 s per instance)."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                names = ["manifest", "keys"] + (["search"] if scope["path"].startswith("/mcp") else [])
                for n in names:
                    await asyncio.to_thread(r2db.pull, rw, n)
            await self.app(scope, receive, send)

    mcp_app = McpApp()
    app = Starlette(lifespan=lifespan, routes=[
        Route("/", root),
        Route("/admin/login", login, methods=["GET", "POST"]),
        Route("/admin/logout", logout, methods=["POST"]),
        Route("/admin", admin, methods=["GET", "POST"]),
        Route("/admin/recordings", recordings),
        Route("/mcp", mcp_app),
        Route("/mcp/{key}", mcp_app),
        Route("/api/cron/sync", cron_sync),
        Route("/api/health", health),
    ], middleware=[Middleware(Refresh)] if serverless else [])
    app.state.archive = archive
    return app


def main() -> int:
    import uvicorn
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_web_config()
        app = create_app(cfg)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    host, _, port = cfg["bind"].rpartition(":")
    # access_log=False: the MCP key is part of the URL and must never reach a log.
    uvicorn.run(app, host=host or "127.0.0.1", port=int(port), access_log=False, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
