"""Read-only Zoom client (Server-to-Server OAuth). Every call is a GET except the token POST."""
import logging
import re
import threading
import time
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)
BACKOFF = 1.0            # seconds, doubled per retry (tests set it to 0)
MAX_TRIES = 5
MAX_RETRY_AFTER = 300
FORBIDDEN_SCOPE_WORDS = ("delete", "write", "update")
_secrets: set[str] = set()


def redact(text) -> str:
    """Strip tokens, secrets and URLs from anything that may reach a log or last_error."""
    s = re.sub(r"(?i)(bearer\s+|access_token[=:]\s*)[^\s&'\"]+", r"\1[REDACTED]", str(text))
    s = re.sub(r"(?i)https?://[^\s'\"]+", "[url]", s)
    s = re.sub(r"(?i)with url: \S+", "with url: [path]", s)
    for v in _secrets:
        s = s.replace(v, "[REDACTED]")
    return s[:300]


class ZoomError(RuntimeError):
    pass


class HTTPError(ZoomError):
    def __init__(self, status: int, message: str, code: int | None = None):
        self.status, self.code = status, code
        super().__init__(f"HTTP {status}{f' code {code}' if code else ''}: {message}")


class Transient(ZoomError):
    """Network drop or a download that died mid-stream. Worth retrying."""


def encode_uuid(uuid: str) -> str:
    """UUIDs starting with / or containing // are URL-encoded twice (Zoom rule)."""
    once = quote(uuid, safe="")
    return quote(once, safe="") if uuid.startswith("/") or "//" in uuid else once


class Zoom:
    def __init__(self, cfg):
        self.cfg = cfg
        self.scopes: list[str] = []
        self._tok, self._expiry, self._not_before = None, 0.0, 0.0
        self._lock, self._local = threading.Lock(), threading.local()
        _secrets.add(cfg.zoom_client_secret)

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            self._local.s = requests.Session()
        return self._local.s

    def _token(self, stale: str | None = None) -> str:
        with self._lock:
            if self._tok and self._tok != stale and time.time() < self._expiry - 300:
                return self._tok
            data = self.request(self.cfg.zoom_oauth_url, post=True, params={
                "grant_type": "account_credentials", "account_id": self.cfg.zoom_account_id}).json()
            scopes = (data.get("scope") or "").split()
            bad = [s for s in scopes if any(w in s.lower() for w in FORBIDDEN_SCOPE_WORDS)]
            if not scopes or bad:
                raise ZoomError("Refusing to run: " + (f"token has non-read-only scopes {bad}" if bad
                                                       else "could not read the granted scopes"))
            self.scopes, self._tok = scopes, data["access_token"]
            self._expiry = time.time() + float(data.get("expires_in") or 3600)
            _secrets.add(self._tok)
            return self._tok

    def token(self) -> str:
        return self._token()

    def request(self, url: str, params=None, stream=False, post=False) -> requests.Response:
        """200 response or an exception. 429 honours Retry-After, 5xx/network retry with backoff,
        401 refreshes the token once."""
        refreshed = False
        for attempt in range(1, MAX_TRIES + 1):
            if (pause := self._not_before - time.time()) > 0:  # a 429 on any thread slows all of them
                time.sleep(pause)
            last, tok = attempt == MAX_TRIES, None
            if post:
                kw = {"auth": (self.cfg.zoom_client_id, self.cfg.zoom_client_secret)}
            else:
                tok = self._token()
                kw = {"headers": {"Authorization": f"Bearer {tok}"}}
            try:
                r = self._session().request("POST" if post else "GET", url, params=params, stream=stream,
                                            timeout=(15, 120), **kw)
            except requests.RequestException as e:
                if last:
                    raise Transient(redact(f"{type(e).__name__}: {e}")) from None
                time.sleep(BACKOFF * 2 ** (attempt - 1))
                continue
            s = r.status_code
            if s == 200:
                return r
            try:
                body = r.json()
            except ValueError:
                body = {}
            msg = redact(body.get("message") or body.get("reason") or r.reason or "")
            retry_after = r.headers.get("Retry-After", "")
            r.close()
            if s == 401 and tok and not refreshed:
                refreshed = True
                self._token(stale=tok)
                continue
            if (s == 429 or s >= 500) and not last:
                wait = min(float(retry_after), MAX_RETRY_AFTER) if s == 429 and retry_after.replace(".", "").isdigit() \
                    else BACKOFF * 2 ** (attempt - 1)
                log.warning("Zoom HTTP %s, retrying in %.1fs", s, wait)
                if s == 429:
                    self._not_before = max(self._not_before, time.time() + wait)
                time.sleep(wait)
                continue
            raise HTTPError(s, msg, body.get("code") if isinstance(body.get("code"), int) else None)
        raise AssertionError("unreachable")

    def get(self, path: str, params=None) -> dict:
        return self.request(self.cfg.zoom_api_base + path, params).json()

    def pages(self, path: str, params: dict, key: str):
        token = ""
        while True:
            p = {**params, "page_size": 300, **({"next_page_token": token} if token else {})}
            d = self.get(path, p)
            yield from d.get(key) or []
            token = d.get("next_page_token") or ""
            if not token:
                return

    def users(self) -> list[dict]:
        out = {}
        for status in ("active", "inactive", "pending"):
            for u in self.pages("/users", {"status": status}, "users"):
                out.setdefault(u["id"], u)
        return list(out.values())

    def recordings(self, user_id: str, frm, to) -> list[dict]:
        return list(self.pages(f"/users/{quote(user_id, safe='@')}/recordings",
                               {"from": frm.isoformat(), "to": to.isoformat()}, "meetings"))

    def trash(self, user_id: str) -> list[dict]:
        return list(self.pages(f"/users/{quote(user_id, safe='@')}/recordings",
                               {"trash": "true", "trash_type": "meeting_recordings"}, "meetings"))

    def meeting_recordings(self, uuid: str) -> dict:
        return self.get(f"/meetings/{encode_uuid(uuid)}/recordings")

    @staticmethod
    def chunks(resp: requests.Response):
        try:
            for c in resp.iter_content(1 << 20):
                if c:
                    yield c
        except (requests.RequestException, OSError) as e:
            raise Transient(redact(f"download interrupted: {type(e).__name__}")) from None
        finally:
            resp.close()
