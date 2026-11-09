# Live Zoom findings (2026-11-09)

Read-only check run with the real Server-to-Server app. Nothing was saved, no secrets or download
URLs were printed. These facts override PRD assumptions where they differ.

| # | Finding | Effect |
|---|---|---|
| Z1 | Token works. Granted scopes: `cloud_recording:read:list_user_recordings:admin`, `cloud_recording:read:list_recording_files:admin`, `cloud_recording:read:recording:admin`. All read-only | Startup scope check passes |
| Z2 | **Missing `user:read:list_users:admin`**: `GET /users` → HTTP 400, code 4711 | John adds the scope. Until then the sync falls back to `ZOOM_USER_EMAILS` (Zoom accepts an email as userId) |
| Z3 | All 3 users are reachable by email: john@ 34 meetings / 13.3 GB, PM@ 101 / 43.1 GB, zoom.buddy@ 37 / 9.7 GB (last 30 days) | One app covers the whole account (PRD risk 3 cleared) |
| Z4 | **Download method A (Authorization: Bearer header) works** | `ZOOM_DOWNLOAD_AUTH=auto` will settle on A (PRD risk 1 cleared) |
| Z5 | **Trash: 65 meetings, 36.9 GB, all deleted on 2026-11-06 → purge about 2026-12-06.** PM@ 57 (Jul 7–31, 2026), zoom.buddy@ 8 (Apr 14–23, 2026), john@ 0 | Matches the PRD's 65 |
| Z6 | **Trash items cannot be downloaded through the API.** The Trash listing has one entry per meeting with empty file_type, no status, no file id and no download_url; `GET /meetings/{uuid}/recordings` returns 404 code 3301 "This recording does not exist" | PRD Test 2 fallback applies: John must click **Recover** in the Zoom portal (the API recover call is a write and is forbidden). The sync stores Trash as an inventory and archives the meetings once they reappear as active |
| Z7 | `recording_type` can contain parentheses and capitals: `shared_screen_with_speaker_view(CC)` | File names sanitise recording_type (see plan §3.1) |
| Z8 | **SUMMARY files exist** (AI Companion): `summary` (JSON: `overall_summary`, `items[{label,start_time,end_time,summary,short_summary}]`) and `summary_next_steps` (JSON with a `markdown` field). Seen on john@ meetings | Archived and shown to the AI through get_meeting (decision D7) |
| R1 | R2 token (Object Read & Write, `flolabs-zoom-archive` only) works: list, write `_spike/token-check.txt`, head size and read-back hash all OK; listing other buckets is denied | PRD Phase 1 "done" check passes |
| R2 | **The bucket already holds `recordflow/`: 560 objects, 45.96 GB, written 2026-11-07 .. 2026-11-09** (RecordFlow pilot; layout `recordflow/<yyyy>/<date-time-topic>/<n>-<type>.<ext>`, includes .docx transcripts). It was still being written on 2026-11-09 | Our code never writes, reads or deletes `recordflow/`. verify must count only our prefixes (`meetings/`, `restricted/`), not the whole bucket, or the PRD "bucket totals within 1%" check fails by ~46 GB. RecordFlow should be stopped and its Zoom authorization removed (PRD Phase 0; it has an optional delete scope). What to do with the 46 GB is John's call |
| Z9 | Types seen: MP4 (gallery, speaker, speaker(CC)), M4A, TRANSCRIPT, CC, CHAT, TIMELINE, SUMMARY ×2. Every meeting has M4A and TIMELINE; transcripts on ~70% | Naming table complete |
