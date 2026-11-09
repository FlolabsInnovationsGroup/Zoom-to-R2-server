-- Local SQLite schema (KEYS_DB_PATH, created by sync/zoom_archive/access.py): the admin page writes api_keys,
-- the MCP server reads api_keys and writes last_used_at + access_log. (Name kept from the D1 days.)

CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,            -- uuid v4
  name TEXT NOT NULL,             -- human label, e.g. "Ahsan - Claude Code"
  key_prefix TEXT NOT NULL,       -- first 12 chars of the key, shown in the UI
  key_hash TEXT NOT NULL UNIQUE,  -- lowercase hex sha256 of the full key string (UTF-8)
  access_groups TEXT NOT NULL,    -- JSON array of group ids from contracts/access_groups.json
                                  -- ("member", "buddy", "admin"), non-empty
  created_by TEXT NOT NULL,       -- admin email from Cloudflare Access
  created_at TEXT NOT NULL,       -- ISO 8601 UTC, e.g. 2026-11-09T10:00:00Z
  expires_at TEXT,                -- NULL = never expires
  revoked_at TEXT,                -- NULL = active
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS access_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id TEXT NOT NULL,
  at TEXT NOT NULL,               -- ISO 8601 UTC
  tool TEXT NOT NULL,             -- list_meetings | get_meeting | get_transcript | search | fetch
  meeting_key TEXT,               -- NULL for list/search calls
  result TEXT NOT NULL            -- ok | denied | not_found | error
);

CREATE INDEX IF NOT EXISTS idx_access_log_key_at ON access_log (key_id, at);
