-- Shared contract: sync/ (zoom_archive) creates and writes this SQLite file.
-- admin/ (archive_admin) opens it READ-ONLY (file:...?mode=ro).
-- Journal mode must stay the default rollback journal (DELETE), not WAL, so read-only opening works.
-- Columns marked [PRD] are from PRD Phase 4; the rest were added for naming and the admin UI.

CREATE TABLE IF NOT EXISTS files (
  zoom_file_id    TEXT PRIMARY KEY,          -- [PRD]
  meeting_uuid    TEXT NOT NULL,             -- [PRD]
  meeting_id      INTEGER NOT NULL,          -- [PRD]
  host_email      TEXT,                      -- [PRD]
  topic           TEXT,                      -- [PRD] raw Zoom topic, any script
  account         TEXT NOT NULL,             -- john | pm | buddy (contracts/access_groups.json accounts),
                                             -- else a slug of the host email's local part
  team            TEXT NOT NULL,             -- [PRD] team slug from teams.yaml
  source          TEXT NOT NULL,             -- [PRD] active | trash
  recording_type  TEXT,                      -- [PRD]
  file_type       TEXT,                      -- [PRD]
  zoom_size       INTEGER,                   -- [PRD] bytes, as Zoom reports
  r2_key          TEXT,                      -- [PRD] full object key
  r2_size         INTEGER,                   -- [PRD]
  sha256          TEXT,                      -- [PRD] lowercase hex
  status          TEXT NOT NULL,             -- [PRD] pending | uploaded | verified | failed
  attempts        INTEGER NOT NULL DEFAULT 0,-- [PRD]
  last_error      TEXT,                      -- [PRD] short reason, never a token or a full download URL
  updated_at      TEXT,                      -- [PRD] ISO 8601 UTC
  meeting_key     TEXT NOT NULL,             -- see IMPLEMENTATION_PLAN §3.1
  start_time_utc  TEXT NOT NULL,             -- meeting start, ISO 8601 UTC
  duration_min    INTEGER,
  recording_start TEXT,                      -- file segment start, used for -2/-3 ordering
  unmapped        INTEGER NOT NULL DEFAULT 0 -- 1 = no teams.yaml rule matched (fell to default_team)
);

CREATE INDEX IF NOT EXISTS idx_files_meeting ON files (meeting_uuid);
CREATE INDEX IF NOT EXISTS idx_files_status  ON files (status);
CREATE INDEX IF NOT EXISTS idx_files_team    ON files (team, start_time_utc);

-- Zoom Trash inventory (docs/ZOOM_FINDINGS.md Z6): Trash items cannot be downloaded, so each run
-- records what is in Trash. When John recovers a meeting in the portal it shows up in the normal
-- listing, gets archived with source='trash', and archived becomes 1.
CREATE TABLE IF NOT EXISTS trash_items (
  meeting_uuid    TEXT PRIMARY KEY,
  meeting_id      INTEGER,
  host_email      TEXT,
  topic           TEXT,
  start_time_utc  TEXT,
  total_size      INTEGER,                   -- bytes, as the Trash listing reports
  deleted_time    TEXT,                      -- ISO 8601 UTC; purge is about deleted_time + 30 days
  first_seen_at   TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,             -- last run that still saw it in Trash
  archived        INTEGER NOT NULL DEFAULT 0 -- 1 once every file of the recovered meeting is verified
);

-- Transcript word count per meeting (D16): meetings under MIN_TRANSCRIPT_WORDS (default 300) are not copied;
-- their file rows get status 'skipped'. Counted once from the Zoom transcript during the scan.
CREATE TABLE IF NOT EXISTS meeting_words (
  meeting_uuid    TEXT PRIMARY KEY,
  words           INTEGER NOT NULL,          -- 0 = the meeting has no transcript
  counted_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id          TEXT PRIMARY KEY,          -- [PRD]
  mode            TEXT NOT NULL,             -- [PRD] backfill | sync | scan | reindex | verify | check
  started_at      TEXT NOT NULL,             -- [PRD]
  finished_at     TEXT,                      -- [PRD] NULL while running or after a crash
  files_done      INTEGER NOT NULL DEFAULT 0,-- [PRD] newly verified this run
  files_failed    INTEGER NOT NULL DEFAULT 0,-- [PRD]
  bytes           INTEGER NOT NULL DEFAULT 0,-- [PRD] bytes uploaded this run
  files_skipped   INTEGER NOT NULL DEFAULT 0,-- already verified, or still processing in Zoom
  exit_code       INTEGER,                   -- 0 ok, non-zero if any file failed
  unmapped_topics TEXT                       -- JSON array of topics that fell to default_team
);
