# Contracts

R2 holds only `pm-account/`, `john-account/`, `buddy-account/` and `_system/manifest.sqlite` (D15).

Shapes shared by `sync/` (the sync, the admin page and the MCP server). Each component's tests load these files, so a
change here must be made on purpose and in every component. Spec: `docs/IMPLEMENTATION_PLAN.md` §3.

| File | Used by | What |
|---|---|---|
| `access_groups.json` | sync, web | Zoom account → folder slug (`john`, `pm`, `buddy`) and the 3 key access groups (Member, Buddy, Admin) with their allow rules |
| `manifest_schema.sql` | sync | SQLite manifest (`files`, `trash_items`, `runs`; run modes backfill, sync, scan, reindex, verify, check) |
| `status_example.json` | sync, web | Shape of the status dict (`outputs.status_json`) the admin page builds live from the manifest; nothing is written to R2 any more |
| `fixtures/summary.json`, `fixtures/summary_next_steps.json` | web | AI Companion summary files (shapes from the live account) |
| `d1_schema.sql` | web | local SQLite tables `api_keys` (with `access_groups`), `access_log` (KEYS_DB_PATH) |
| `key_vectors.json` | web | Key format + hash test vectors |
| `meeting.schema.json` | sync, web | meeting.json schema (folder `<account>-account/<team>/<yyyy>/<folder>/`, no `restricted` field) |
| `fixtures/meeting.json` | sync, web | Design Team, 4 Nov 2026 (4 files) |
| `fixtures/all_meetings.json` | sync, web | Full meeting.json objects of the fixtures (pm-account, buddy-account, john-account), for seeding R2 |
| `fixtures/transcript.vtt` | web | Zoom-style VTT: speakers, multi-line cue, Arabic, no-speaker cue, >1h timestamp |
| `fixtures/transcript.expected.txt` | web | Exact parsed output of the VTT above (`[HH:MM:SS] Speaker: text`, cue lines joined by a space) |

Fixture sizes and sha256 values are made up. Only their shape matters.
