# Implementation Plan: Zoom → R2 Archive, Admin UI, MCP Server

Source of truth for requirements: [`PRD.md`](PRD.md). This file records decisions taken after the
PRD (they override it where they differ), the contracts shared between components, and how the
build is split up.

## 1. Decisions (locked 2026-11-09)

| # | Decision | Overrides PRD? |
|---|---|---|
| D1 | **Naming:** PRD folder layout, but file names carry topic and date: `<topic-slug>_<DD-MM-YYYY>_<kind>.<ext>` | Yes (file names only) |
| D2 | **AI access by API key, scoped to team folders.** Admins create keys in the admin UI and tick which teams a key may read. Claude.ai / ChatGPT connect through OAuth: the sign-in page asks for the API key. Claude Code can send the key directly as `Authorization: Bearer`. | Yes (replaces the sign-in allowlist) |
| D3 | **Restricted meetings live in the same bucket** under `restricted/`. The Worker reads by allowlist (only `index/meetings.jsonl` and `meetings/<allowed-team>/...`), so `restricted/`, `state/`, `reports/` and `_spike/` can never be read. A test proves it. | Picks the PRD's "prefix" option |
| D4 | ~~Admin UI on Hetzner via Tunnel~~ **Replaced 2026-11-09 (simplify):** the admin page is a small `/admin` route **inside the MCP Worker**, behind Cloudflare Access (admin emails). Same D1 binding for keys; sync status comes from `state/status.json` that the sync writes to R2 (`contracts/status_example.json`). No admin server, no Tunnel. `contracts/admin_views.md` is obsolete. | Yes (PRD had no UI) |
| D13 | **Keep it simple** (user, 2026-11-09): one Python sync script on the server + one Worker (MCP + admin page). Small focused test set, not the full §4 matrix. | |
| D14 | **One Hetzner server runs sync + admin + MCP; Caddy + own domain for HTTPS; admin password login; MCP key in URL; keys in local SQLite.** Replaces the Cloudflare Worker (D4). | |
| D5 | Sync script language: **Python 3.12** (as the PRD says). MCP Worker: **TypeScript**. Admin UI: **Python (FastAPI + server-rendered HTML/CSS, no JS framework)**. | No |
| D6 | The 3 "accounts" are **one Zoom account with 3 users** (PRD fact). One Server-to-Server app covers all of them. | n/a |
| D7 | **AI Companion summaries are archived and shown to the AI** (`summary`, `summary_next_steps` JSON; see `ZOOM_FINDINGS.md` Z8). get_meeting returns them as untrusted content. | Yes (PRD said not reachable) |
| D8 | **Users:** `GET /users` once John adds `user:read:list_users:admin`. If that call fails with a scope error, fall back to `ZOOM_USER_EMAILS` (comma-separated) and say so in the run summary. | Adds a fallback |
| D9 | **Trash is inventory only** (Z6): Trash items can't be downloaded. Each run records them in `trash_items`; once John recovers them in the portal they appear in the normal listing and get archived with `source = "trash"`. The admin UI shows Trash meetings, GB, archived count and days to purge. | Yes |
| D11 | **One folder per Zoom account, access by 3 groups** (replaces team-scoped keys in D2). Layout `meetings/<account>/<team>/...` with account `john`, `pm`, `buddy`. Each API key gets any combination of the groups in `contracts/access_groups.json`: **Member** = PM meetings except Legal (`ethics-legal`), Project Management and Operations; **Buddy** = all zoom.buddy meetings; **Admin (John)** = John's meetings plus those 3 PM teams. Ticked/unticked per key in the UI. `restricted/` (HR, 1:1s) is never readable by any key, Admin included. Accounts not in `access_groups.json` are archived but no group covers them; the Status page flags them. | Yes |
| D12 | **Full backfill**: every recording back to 2020-01-01 (~325 GB), not a sample. Plus the 65 Trash recordings once John recovers them. | No (PRD) |
| D10 | **Local development first**, Hetzner later. Dev runs use the real R2 bucket under a dev prefix-free layout (small `--limit` runs; R2 has no rename, so team mapping still applies). Tests use moto. Admin UI locally: `APP_ENV=dev` + `ADMIN_DEV_EMAIL` skips Cloudflare Access **only** when bound to 127.0.0.1. MCP Worker locally: `wrangler dev` with local R2/D1. | Adds dev mode |

Still open and owned by John (they do **not** block the code, only the full backfill in Phase 6):
the 3 team-mapping questions, the list of restricted topics, who gets API keys, the budget, and the
second backup copy. They are config (`teams.yaml`) or operations, not code.

## 2. Repository layout (simplified, D13)

```
docs/        PRD.md, IMPLEMENTATION_PLAN.md, ZOOM_FINDINGS.md
contracts/   shapes shared by sync and worker (see contracts/README.md)
sync/        Python: Zoom -> R2 archiver (check | backfill | sync | verify), runs on the server
worker/      Cloudflare Worker: read-only MCP server at /mcp + small admin page at /admin
```

> **Simplified 2026-11-09.** Sections 3.6, 4 and 5 below describe the earlier, larger build
> (separate admin server, full test matrix, 4 workflows). They are kept for history only; D4 and
> D13 supersede them.

## 3. Contracts (shared, fixed before the build)

All live in `contracts/`. Each component has tests that load these files, so a mismatch fails CI.

### 3.1 R2 key layout

> **D11 update:** every path below has an `<account>/` segment before `<team>/`, e.g.
> `meetings/pm/design/2026/...`, and `meeting_key` = `<account>/<team>/<yyyy>/<folder>`. The
> fixtures and `meeting.schema.json` are authoritative. `account` comes from
> `contracts/access_groups.json` `accounts` (host email, case-insensitive), else a slug of the
> email's local part.

```
meetings/<account>/<team>/<yyyy>/<YYYY-MM-DD>_<HHMM>utc_<topic-slug>_<uuid8>/
    <topic-slug>_<DD-MM-YYYY>_video-<recording_type>.mp4
    <topic-slug>_<DD-MM-YYYY>_audio.m4a
    <topic-slug>_<DD-MM-YYYY>_transcript.vtt
    <topic-slug>_<DD-MM-YYYY>_captions.vtt
    <topic-slug>_<DD-MM-YYYY>_chat.txt
    <topic-slug>_<DD-MM-YYYY>_timeline.json
    <topic-slug>_<DD-MM-YYYY>_summary.json              (SUMMARY / summary)
    <topic-slug>_<DD-MM-YYYY>_summary_next_steps.json   (SUMMARY / summary_next_steps)
    <topic-slug>_<DD-MM-YYYY>_<type-slug>.<ext>          (any other type)
    meeting.json
restricted/<team>/<same structure>
restricted/index/meetings.jsonl          restricted meetings only
index/meetings.jsonl                     NON-restricted meetings only
index/meetings.csv                       NON-restricted meetings only
state/manifest.sqlite
reports/verification-<YYYY-MM-DD>.md
_spike/                                  Phase 5 test 1 uploads
```

Rules:
- All dates and times in the path are **UTC** (start time of the meeting). The DD-MM-YYYY in the
  file name is the same UTC date as the folder.
- `topic-slug`: NFKD-normalise, drop non-ASCII, lowercase, any run of non `[a-z0-9]` → `-`, trim
  `-`, cut to 60 chars and re-trim. Empty → `meeting-<zoom_meeting_id>`.
- `uuid8`: first 8 hex chars of `sha256(zoom_uuid)` (Zoom UUIDs contain `/`, `+`, `=`).
- `type-slug` / recording_type in `video-<recording_type>`: lowercase, any run of characters
  outside `[a-z0-9_]` → `-`, trim `-`. So `shared_screen_with_speaker_view(CC)` →
  `video-shared_screen_with_speaker_view-cc.mp4`. `<ext>` = lowercased `file_extension`.
- Several files of the same kind in one meeting: first keeps the plain name, then `-2`, `-3`
  before the extension (`..._transcript-2.vtt`), ordered by `recording_start`, ties by
  `zoom_file_id`.
- **meeting_key** (what MCP tools use) = the folder path without the `meetings/` root and without
  the trailing slash, e.g. `design/2026/2026-11-04_1400utc_design-team_ab12cd34`. Must match
  `^[a-z0-9-]+/\d{4}/\d{4}-\d{2}-\d{2}_\d{4}utc_[a-z0-9-]+_[0-9a-f]{8}$`.

### 3.2 meeting.json and index line

`contracts/meeting.schema.json` (JSON Schema). Same as the PRD object plus:
- `meeting_key` (string, as above) and `folder` (full R2 prefix with trailing `/`)
- `files[].name` is the file name only; `files[].r2_key` is the full key
- `start_time_cairo` is ISO 8601 with offset, Africa/Cairo
- An index line is the same object **without `files`**, plus `has_transcript` (bool).

Example fixtures: `contracts/fixtures/meeting.json`, `contracts/fixtures/meetings.jsonl`,
`contracts/fixtures/transcript.vtt`.

### 3.3 teams.yaml

```yaml
default_team: misc
rules:                 # top to bottom, first match wins
  - team: design
    contains: ["design team"]          # case-insensitive substring of the topic
  - team: hr
    restricted: true
    contains: ["hr team meeting"]
    regex: ["\\b1\\s*(to|:|-)\\s*1\\b"]  # optional, case-insensitive
    meeting_ids: []                     # optional, exact Zoom meeting IDs John marks
```

Restricted teams go under `restricted/`. They are never offered in the admin UI's key form.

### 3.4 D1 schema (admin UI writes, Worker reads)

`contracts/d1_schema.sql`:

```sql
CREATE TABLE api_keys (
  id TEXT PRIMARY KEY,            -- uuid v4
  name TEXT NOT NULL,             -- "Ahsan - Claude Code"
  key_prefix TEXT NOT NULL,       -- first 12 chars of the key, shown in the UI
  key_hash TEXT NOT NULL UNIQUE,  -- lowercase hex sha256 of the full key string
  teams TEXT NOT NULL,            -- JSON array of team slugs, non-empty, no restricted team
  created_by TEXT NOT NULL,       -- admin email from Cloudflare Access
  created_at TEXT NOT NULL,       -- ISO 8601 UTC
  expires_at TEXT,                -- NULL = never
  revoked_at TEXT,                -- NULL = active
  last_used_at TEXT
);
CREATE TABLE access_log (         -- one row per MCP tool call
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id TEXT NOT NULL, at TEXT NOT NULL, tool TEXT NOT NULL,
  meeting_key TEXT, result TEXT NOT NULL   -- ok | denied | not_found | error
);
```

Key string: `fma_` + 43 chars base64url (32 random bytes). Hash vectors in
`contracts/key_vectors.json` are checked by both admin and Worker tests.

### 3.5 MCP surface (Worker)

Endpoint `/mcp` (streamable HTTP). Auth, in order:
1. `Authorization: Bearer fma_...` → look up `sha256(key)` in D1.
2. Otherwise an OAuth token issued by `@cloudflare/workers-oauth-provider`. Its `/authorize` page
   is a single form: paste API key → checked in D1 → grant carries `{ keyId }`.

On **every** tool call the key row is re-read: revoked or expired → 401. So revoking a key in
the UI also kills OAuth sessions made with it.

**Access check (D11):** a meeting is readable if any of the key's `access_groups` has an `allow`
rule in `contracts/access_groups.json` matching its `account` and `team` (`teams` = only these,
`teams_except` = all but these, neither = whole account). Never anything under `restricted/`.
Test matrix with the fixtures: Member reads pm/design and pm/frontend, not pm/operations, buddy or
john. Buddy reads only buddy/. Admin reads john/humanoid and pm/operations, not pm/design. No key
reads restricted/john/hr.

| Tool | Input | Output |
|---|---|---|
| list_meetings | team?, from?, to?, query?, limit (≤100), cursor? | Lines: meeting_key, date, team, topic, duration, has_transcript. Only allowed teams |
| get_meeting | meeting_key | meeting.json without r2 keys or hashes, plus `summary` when the meeting has AI Companion files: `{overall_summary, sections:[{label,start_time,summary}], next_steps_markdown}` (from `_summary.json` / `_summary_next_steps.json`), inside the same untrusted-content label |
| get_transcript | meeting_key, cursor? | `[HH:MM:SS] Speaker: text` lines, pages ≤ 30,000 chars, wrapped in an "untrusted meeting content" label |
| search | query | ChatGPT shape: `{results:[{id,title,url}]}` over index metadata (topic, team, date) |
| fetch | id | ChatGPT shape: `{id,title,text,url,metadata}`, text = full transcript |

A meeting outside the key's teams returns the **same** "not found" as a missing one. The R2
binding is wrapped so only `get`, `head` and `list` exist in code.

### 3.6 Admin UI

Pages: **Status** (runs, counts by status with GB, Trash progress, failed files with reason,
unmapped topics), **Meetings** (from the manifest: date, team, topic, host, files
verified/total, size, restricted flag; filters), **API keys** (list, create with team checkboxes,
key shown once with copy button, revoke).

- Binds to `127.0.0.1` only. cloudflared publishes it.
- Every request checks the `Cf-Access-Jwt-Assertion` JWT (signature against the team's certs,
  `aud`, expiry) **and** that the email is in `ADMIN_EMAILS`. Fails closed.
- POST forms carry a CSRF token.
- Opens the manifest with `file:...?mode=ro`. The manifest uses the default rollback journal, not
  WAL, so read-only opening works.
- Talks to D1 through the Cloudflare REST API with a token scoped to that one database.
- Design: dense, calm ops dashboard. Plain HTML + one CSS file, light/dark, system font stack,
  tabular numbers, no external requests.

## 4. Test strategy (real-world scenarios)

No live Zoom or R2 in CI. The fakes follow the real API shapes from the PRD:
- **sync:** a fake Zoom (in-process HTTP mock with stateful handlers) plus a local S3 (moto server).
- **worker:** `@cloudflare/vitest-pool-workers` with real R2/D1 bindings from miniflare, seeded
  from `contracts/fixtures`.
- **admin:** FastAPI TestClient, a fixture manifest, and a fake D1 REST endpoint.

Scenarios that must exist as tests:

| Area | Scenario |
|---|---|
| sync | Backfill of 3 users with Trash first: manifest count == fake Zoom count == R2 objects |
| sync | Second run uploads 0 files |
| sync | Stream dies mid-file: multipart aborted, nothing verified, rerun completes |
| sync | Zoom says 1,000 bytes and sends 900: failed, never verified |
| sync | 429 with Retry-After: waits, then succeeds, concurrency drops by one |
| sync | 401: one token refresh, then success; second 401 fails the file |
| sync | Token older than 55 min is refreshed before use |
| sync | File still `processing`: skipped, picked up on the next run |
| sync | Two transcript segments: `_transcript.vtt` and `_transcript-2.vtt` by recording_start |
| sync | UUID with `/` or `//`: double-encoded in the request path |
| sync | Arabic-only topic → `meeting-<id>` slug, real topic kept in meeting.json |
| sync | HR meeting → `restricted/`, absent from `index/meetings.jsonl`, present in the restricted index |
| sync | Unmapped topic → `misc` and listed in the run summary |
| sync | Range over several months → one request per month, `next_page_token` followed |
| sync | Token whose scopes contain delete/write/update → refuses to start |
| sync | Object already in R2 with the right size → verified without upload |
| sync | Wrong R2 key → every file fails, none verified, exit code ≠ 0 |
| sync | 404 on download → failed with reason, run continues, exit code ≠ 0 |
| sync | verify finds a meeting missing from R2 and a size mismatch |
| sync | `sync --lookback-days 7` only lists the last 7 days plus Trash |
| sync | No DELETE/PUT/PATCH to Zoom and no `delete_object` anywhere in the code (static test) |
| sync | Logs never contain the token or an `access_token=` URL |
| worker | Design-only key lists only design meetings |
| worker | Design key asks for a frontend meeting → "not found", same text as a missing one |
| worker | meeting_key with `..`, `restricted/`, `state/` or a bad shape → rejected |
| worker | Revoked key and expired key → 401 (direct bearer and OAuth both) |
| worker | One-hour transcript > 30k chars → pages joined back equal the full text |
| worker | VTT with speaker labels, multi-line cues and Arabic text parses correctly |
| worker | Transcript output carries the untrusted-content label |
| worker | `search` / `fetch` return ChatGPT shapes |
| worker | Each tool call writes an access_log row |
| admin | No JWT, bad JWT, wrong aud, or email not in ADMIN_EMAILS → 403 |
| admin | Create key: shown once, only the hash reaches D1, restricted team or empty team list refused |
| admin | Revoke sets revoked_at; a revoked key is shown as revoked |
| admin | POST without CSRF token → 403 |
| admin | Status page shows failed files and unmapped topics from a fixture manifest |
| admin | Manifest is read-only to the UI (a write attempt fails) |
| cross | Key vectors: same key → same hash in admin (Python) and worker (TS) |
| cross | meeting.json written by sync validates against the schema the worker tests use |

## 5. Build order

1. **Scaffold + contracts** (done by hand, before the workflows): `contracts/` (also
   `manifest_schema.sql` for sync → admin and `admin_views.md` for backend → UI), `.env.example`.
2. **Build: 4 workflows in parallel**, each owning its own folders:

   | Workflow | Owns |
   |---|---|
   | Zoom connect | `sync/` (Zoom client, R2 upload, manifest, naming, verify, CLI incl. `check` and `spike`) |
   | Cloudflare | `worker/` (MCP + OAuth + D1 + R2 binding), `deploy/cloudflare/` (Tunnel, Access, wrangler steps) |
   | Backend | `admin/` except templates/static (FastAPI, Access JWT, CSRF, D1 REST, manifest read), `deploy/systemd/`, `deploy/RUNBOOK.md` |
   | UI | `admin/archive_admin/templates/`, `admin/archive_admin/static/`, `admin/ui_preview/` (uses the Apple HIG + motion design skills) |

   Each reads this plan + PRD + contracts, writes tests from §4, and **does not make new product
   decisions**: anything unspecified is listed as an open question in its report. Each workflow is
   build → review → fix.
3. **Integrate:** run every test suite, check the cross-component contracts, fix mismatches only.
4. **Review:** security review (read-only guarantees, auth, restricted data, secrets in logs) and
   UI design review, in parallel.
5. **Fix:** apply the confirmed review findings, rerun tests.
6. Report back with results, open questions, and the operator steps (PRD Phases 0–3) that need
   people: Cloudflare, Zoom app, Hetzner.

## 6. Out of scope for this build

- Live runs against Zoom or R2 (need John's Zoom app and rotated Cloudflare keys, Phase 0–2).
- v2 full-text search in D1 FTS5 (`search_transcripts`), video download links, download method C
  (General OAuth app). Method C gets a clear error and a hook, not an implementation.
- Any delete path anywhere.

## D15 (2026-11-09): 3 account folders, no restricted area, start/stop, token-saving MCP tools

Supersedes the restricted/ parts of D11 and 3.1, and the index/ + state/ objects.

1. **R2 layout.** Top level is exactly `pm-account/`, `john-account/`, `buddy-account/` (`<account>-account/`), plus one
   hidden `_system/manifest.sqlite` (backup of the manifest, written at the end of each run).
   Meeting folder: `<account>-account/<team>/<yyyy>/<YYYY-MM-DD>_<HHMM>utc_<slug>_<uuid8>/` with the files and `meeting.json`.
   `meeting_key` is unchanged (`<account>/<team>/<yyyy>/<folder>`); `naming.r2_folder(meeting_key)` gives the R2 folder.
   No `meetings/`, `restricted/`, `index/`, `state/` or `reports/` objects any more (verify writes its report locally).
2. **No restricted concept.** HR (`hr`) and 1:1 (`one-on-one`) are ordinary teams. Access is only the groups in
   contracts/access_groups.json: Member = PM account except ethics-legal, project-management, operations, hr, one-on-one;
   Buddy = buddy account; Admin = every account and team (`{"account": "*"}`).
3. **The meeting list comes from the local manifest** (meetings whose files are all verified), read by the web app and
   MCP. Full-text search: `sync/zoom_archive/search.py` (FTS5, `search.sqlite` next to the manifest, env
   `SEARCH_DB_PATH` optional). The sync indexes each TRANSCRIPT and SUMMARY file after it verifies;
   `--mode reindex` rebuilds it from R2.
4. **Run control** (`sync/zoom_archive/runctl.py`): `sync.alive` heartbeat, `sync.stop` request, `sync.log`, all next to
   the manifest. A run refuses to start while another is alive. SIGTERM acts like Ctrl-C.
5. **Backfill scans first:** list every meeting 2020 -> today for all users and upsert every ready file as pending (the
   full total is known in minutes), then download pending files newest first. `--mode scan` = the listing only.
6. **Admin page:** Start backfill / Scan Zoom / Stop; auto-refresh while a run is live; last lines of sync.log;
   per-account archived %; `/admin/recordings` lists every meeting with its status, filter by account and status.
7. **MCP tools** (compact text, server does the searching so the AI spends few tokens): list_accounts_and_teams,
   list_meetings (account, team, from, to, month, query, has_transcript), search_transcripts (FTS snippets with
   timestamps), get_meeting, get_summary, find_in_transcript, get_transcript (pages, optional time window),
   get_recording_links (presigned R2 GET links, 1 hour), plus search / fetch for ChatGPT.
