"""Config from the env names in .env.example. Test-only overrides: ZOOM_API_BASE, ZOOM_OAUTH_URL,
R2_ENDPOINT_URL. The repo-root .env is loaded unless ZOOM_ARCHIVE_NO_DOTENV=1."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import search

SYNC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SYNC_DIR.parent
CONTRACTS = REPO_ROOT / "contracts"


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    zoom_account_id: str
    zoom_client_id: str
    zoom_client_secret: str = field(repr=False)
    zoom_user_emails: list
    zoom_api_base: str
    zoom_oauth_url: str
    r2_endpoint: str
    r2_access_key_id: str
    r2_secret_access_key: str = field(repr=False)
    r2_bucket: str
    manifest_path: str
    teams_yaml: str
    concurrency: int
    search_db_path: str


def load_config() -> Config:
    if not os.environ.get("ZOOM_ARCHIVE_NO_DOTENV"):
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")  # never overrides variables that are already set
    g = lambda k, d="": (os.environ.get(k) or d).strip()  # noqa: E731
    endpoint = g("R2_ENDPOINT_URL") or f"https://{g('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
    cfg = Config(
        g("ZOOM_ACCOUNT_ID"), g("ZOOM_CLIENT_ID"), g("ZOOM_CLIENT_SECRET"),
        [e.strip() for e in g("ZOOM_USER_EMAILS").split(",") if e.strip()],
        g("ZOOM_API_BASE", "https://api.zoom.us/v2").rstrip("/"),
        g("ZOOM_OAUTH_URL", "https://zoom.us/oauth/token"),
        endpoint, g("R2_ACCESS_KEY_ID"), g("R2_SECRET_ACCESS_KEY"), g("R2_BUCKET", "zoom-archive-test"),
        g("MANIFEST_PATH", str(REPO_ROOT / ".local" / "manifest.sqlite")),
        g("TEAMS_YAML", str(SYNC_DIR / "teams.yaml")),
        int(g("SYNC_CONCURRENCY", "3") or 3), "")
    cfg.search_db_path = g("SEARCH_DB_PATH") or search.default_path(cfg.manifest_path)
    missing = [n for n, v in (("ZOOM_ACCOUNT_ID", cfg.zoom_account_id), ("ZOOM_CLIENT_ID", cfg.zoom_client_id),
                              ("ZOOM_CLIENT_SECRET", cfg.zoom_client_secret),
                              ("R2_ACCESS_KEY_ID", cfg.r2_access_key_id),
                              ("R2_SECRET_ACCESS_KEY", cfg.r2_secret_access_key)) if not v]
    if missing:
        raise ConfigError("Missing environment variables: " + ", ".join(missing) + " (see .env.example)")
    return cfg


@dataclass
class R2Settings:
    """What R2() needs; also what the web app reads (no Zoom credentials needed there)."""
    r2_endpoint: str
    r2_access_key_id: str
    r2_secret_access_key: str = field(repr=False)
    r2_bucket: str


def load_web_config() -> dict:
    """Settings for the admin page + MCP server. Raises ConfigError with a clear message when something is missing."""
    if not os.environ.get("ZOOM_ARCHIVE_NO_DOTENV"):
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    g = lambda k, d="": (os.environ.get(k) or d).strip()  # noqa: E731
    endpoint = g("R2_ENDPOINT_URL") or f"https://{g('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
    cfg = {
        # The web app reads R2 only. With R2_READ_* (an "Object Read only" token) set, it and every presigned link
        # use that key; the read-write key then stays in the sync's environment.
        "r2": R2Settings(endpoint, g("R2_READ_ACCESS_KEY_ID") or g("R2_ACCESS_KEY_ID"),
                         g("R2_READ_SECRET_ACCESS_KEY") or g("R2_SECRET_ACCESS_KEY"), g("R2_BUCKET", "zoom-archive-test")),
        "admin_password": g("ADMIN_PASSWORD"), "secret_key": g("ADMIN_SECRET_KEY"),
        "manifest_path": g("MANIFEST_PATH", str(REPO_ROOT / ".local" / "manifest.sqlite")),
        "keys_db_path": g("KEYS_DB_PATH", str(REPO_ROOT / ".local" / "keys.sqlite")),
        "bind": g("ADMIN_BIND", "127.0.0.1:8080"),
    }
    cfg["search_db_path"] = g("SEARCH_DB_PATH") or search.default_path(cfg["manifest_path"])
    missing = [n for n, v in (("ADMIN_PASSWORD", cfg["admin_password"]), ("ADMIN_SECRET_KEY", cfg["secret_key"]),
                              ("R2_ACCESS_KEY_ID", cfg["r2"].r2_access_key_id),
                              ("R2_SECRET_ACCESS_KEY", cfg["r2"].r2_secret_access_key)) if not v]
    if missing:
        raise ConfigError("The web app will not start. Set these environment variables: " + ", ".join(missing)
                          + " (see .env.example)")
    return cfg
