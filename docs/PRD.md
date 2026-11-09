# FloLabs Zoom to Cloudflare R2 Archive and AI Access: PRD and Build Guide

Nov 7, 2026 · @FloLabs

> Saved verbatim from the PRD shared in chat on 2026-11-09. The pasted copy was cut off partway
> through Appendix C (pseudocode); everything before that point is complete. Decisions taken
> after this PRD, which override it where they differ, are in `IMPLEMENTATION_PLAN.md`.

## Summary

We copy every Zoom cloud recording and transcript of the FloLabs account into Cloudflare R2, name each meeting by team and date, and let Claude and ChatGPT answer questions about them. Nothing is deleted from Zoom until a written check passes.

**Problem.** The Zoom account uses 324.92 GB of its 230 GB quota (141%). The portal shows 961 recordings in the last 12 months and 65 more in Trash, each marked "30" days left. The Trash items will be purged if nobody recovers them first.

### Goals

- Archive all recordings and transcripts in R2: video, audio, transcript, captions, chat and timeline files.
- Name every meeting by team, date and topic, so a person can find it without opening it.
- Run one read-only Python script on a Hetzner server, using a Zoom Server-to-Server app created from John Hazlewood's Owner account.
- Keep syncing new meetings after the backfill.
- Connect the archive to Claude and ChatGPT through a read-only MCP server, so the team can ask about meetings without opening Zoom.

### Non-goals

- Deleting anything from Zoom. This project has no delete code and no delete scope.
- Local recordings, AI Companion summaries and My Notes. They are not reachable through the cloud recording API (see Risks).
- Live meeting bots or real-time transcription.

### Success criteria

- Zoom portal count (961 in 12 months, 65 in Trash, plus anything older) equals the script manifest count equals the R2 object count.
- Every file's size in R2 equals the size Zoom reports for it.
- A question in Claude and in ChatGPT about a named meeting, for example the Design Team meeting of Nov 4, 2026, returns an answer taken from its transcript.
- John Hazlewood approves in writing before any Zoom deletion is even discussed (see Phase 9).

## Where we are today

The three Zoom logins are users of one Zoom account, so one Owner-created app can reach all of them. That is the main fact the plan rests on. Other items below are marked as inference or unknown, so the team can check them before building on them.

| Item | What we know | Status |
|---|---|---|
| Account | One Zoom account, 3 users, all Workplace Pro: john@flomadlabs.com (Owner), PM@flomad.com (Admin, "Flo Labs"), zoom.buddy@flolabsinnovations.com (Member) | Read from User Management in John's portal |
| Storage | 324.92 GB used of 230 GB quota (141%) | Read from the portal |
| Recordings | 961 in the last 12 months across all hosts. Stored in the United States | Read from the portal |
| Trash | 65 recordings, each showing 30 in "Deleted in (days)". Mostly July 2026 team meetings hosted by PM@flomad.com and April 2026 meetings hosted by zoom.buddy | Read from the portal. Exact purge date (about 30 days after deletion, early December 2026) is an inference |
| Who trashed them | Unknown | Ask the three users |
| Claude's Zoom connector | Attached to the PM@flomad.com host only. It listed 449 meetings (about 194 GB), so those numbers are probably that host alone | Inference |
| Transcript access | Claude fetched the Nov 4, 2026 Design Team transcript (1,014 lines) through the connector | Tested |
| RecordFlow | Piloted on one account, then dropped for this DIY plan. Its Zoom authorization includes an optional delete scope | Check each account's installed apps and remove it if present |
| Not yet checked | The "Transcripts" and "My Notes" tabs in the portal, and recordings older than 12 months | Open |

The Cloudflare test credentials and the R2 token pasted in an earlier chat are exposed. Rotate them before Phase 1 (see Security).

## Requirements and hard rules

The rules in the first list override everything else in this document. If a step seems to conflict with one of them, stop and ask John.

### Hard rules

- Nothing is deleted from Zoom. The script has no delete code. The Zoom app is never given a delete scope (any scope containing delete). Nobody empties Zoom Trash, removes recordings or lowers retention settings until Phase 9 passes and John approves in writing.
- Zoom stays the master copy until the verification report is signed off.
- No secrets in chat, docs, tickets or git. Keys live in the server's env file and in Cloudflare's secret store only.
- The AI connection is read-only. The MCP server can list and read. It cannot write, copy or delete in R2.
- Restricted meetings (HR, 1:1s, anything John marks) go where the AI connection cannot read: a second R2 bucket (recommended) or a separate prefix.

### Functional requirements

| ID | Requirement |
|---|---|
| F1 | List every user of the Zoom account and every cloud recording of each user, including recordings in Trash |
| F2 | Download every file of every recording: video, audio, transcript, captions, chat, timeline |
| F3 | Upload each file to R2 under a name built from team, date, time and topic |
| F4 | After each upload, compare the R2 object size with the size Zoom reports. Mismatch means retry, then flag |
| F5 | Write a manifest row per file (status, size, sha256, timestamps, errors). Re-running skips files already done |
| F6 | Write meeting.json for each meeting and one index/meetings.jsonl for the whole archive |
| F7 | Run on a schedule after the backfill and pick up new meetings |
| F8 | Expose list, read and search tools over MCP to Claude and ChatGPT |
| F9 | Produce a reconciliation report: Zoom count vs manifest vs R2 |

### Non-functional requirements

- Idempotent: running the script twice gives the same R2 content.
- Resumable: a crash or a reboot loses at most the file in progress.
- Polite to Zoom: low concurrency (2 to 4 downloads), backoff on rate-limit responses.
- Observable: every run writes a log and a summary (files done, failed, skipped, bytes).

## Architecture and data flow

Data moves one way: Zoom to a Hetzner server to R2, and Claude and ChatGPT read R2 through one read-only Worker. Nothing flows back to Zoom.

[embedded content: architecture · one-way flow, 6 components]

Read it top to bottom. The sync script reads Zoom and writes R2. The Worker reads R2 and answers the AI tools.

- **Zoom to Hetzner.** The script uses the Server-to-Server app (read scopes only) to list and download recordings.
- **Hetzner to R2.** Each file is streamed into R2 and its size is checked against Zoom's. Restricted meetings go to their own bucket.
- **R2 to the Worker.** The Worker has the meetings bucket attached as a binding. It holds no R2 key and cannot see the restricted bucket.
- **Worker to Claude and ChatGPT.** People sign in once through the connector. The tools return text only, never keys and never write access.

## R2 layout and naming

One bucket, one folder per meeting, and the team, date and topic are in the path. The folder name uses UTC so it never changes; Cairo time is stored inside meeting.json. Decide the team mapping before the full backfill, because R2 has no rename: changing a team later means copying every object to a new key.

```
flolabs-zoom-archive/
  meetings/<team-slug>/<year>/<YYYY-MM-DD>_<HHMM>utc_<topic-slug>_<uuid8>/
      video-<recording_type>.mp4
      audio.m4a
      transcript.vtt
      captions.vtt
      chat.txt
      timeline.json
      meeting.json
  restricted/<same structure>        (HR and 1:1s, never exposed to AI; may be a second bucket instead)
  index/meetings.jsonl               (one line per meeting)
  index/meetings.csv
  state/manifest.sqlite              (copy of the sync state, updated after each run)
```

Example for the Design Team meeting of Nov 4, 2026: `meetings/design/2026/2026-11-04_HHMMutc_design-team_ab12cd34/transcript.vtt`. Fill HHMM from the real start time in the recording list.

### File naming

| Zoom file | Name in R2 | Note |
|---|---|---|
| MP4, any view | video-<recording_type>.mp4 | For example video-shared_screen_with_gallery_view.mp4 |
| M4A, audio_only | audio.m4a | |
| TRANSCRIPT, audio_transcript | transcript.vtt | The one the AI tools read |
| CC, closed_caption | captions.vtt | The RecordFlow pilot skipped this type. We keep it |
| CHAT, chat_file | chat.txt | |
| TIMELINE | timeline.json | |
| Same type twice (several segments) | Add -2, -3, ordered by recording start | Never overwrite |

The exact recording_type and file_type values come from the live API response. Check them on the first test meeting and adjust this table if Zoom returns different words.

meeting.json is written next to the files, and every line of index/meetings.jsonl is the same object without the files list.

```json
{
  "zoom_meeting_id": 0,
  "zoom_uuid": "",
  "topic": "Design Team",
  "team": "design",
  "host_email": "",
  "start_time_utc": "2026-11-04T00:00:00Z",
  "start_time_cairo": "",
  "duration_min": 0,
  "source": "active | trash",
  "restricted": false,
  "files": [
    {"name": "transcript.vtt", "recording_type": "audio_transcript", "zoom_file_id": "",
     "zoom_size": 0, "r2_key": "", "r2_size": 0, "sha256": "", "status": "verified"}
  ],
  "archived_at": "",
  "script_version": ""
}
```

**Team mapping.** The script reads teams.yaml, not code. Rules run top to bottom and the first match wins. Anything unmatched goes to misc and is listed in the run report, so no topic is silently lost. The counts come from the 449 meetings Claude's connector listed on the PM@flomad.com host. The saved topic list adds up to 419, so about 30 meetings are not in this table; the unmapped report will catch them.

| Team slug | Zoom topics matched | Meetings seen |
|---|---|---|
| multi-modal-perception | Multi-modal perception | 31 |
| operations | Operations, Mohamed Operations Meeting | 31 |
| moodchanger | Moodchanger, MoodChanger Meeting | 30 |
| humanoid | Humanoid | 27 |
| frontend | Weekly FrontEnd, Urgent Frontend (proposed) | 29 |
| design-marketing-data | Design Marketing & Data Analytics (open question 2) | 26 |
| robotics | Robotics and Dev, Robotics Team meeting (open question 1) | 31 |
| design | Design Team | 24 |
| project-management | Project Management | 23 |
| automation | Automation | 22 |
| gtm-influencers | GTM & Influencers | 22 |
| data-analytics | Data Analytics | 22 |
| iot | Internet of Things | 21 |
| backend | FloBrain Weekly Backend | 20 |
| technical | Technical Meeting (open question 3) | 20 |
| space | Space Team, Spaceventuresinstitute website update (proposed) | 19 |
| ethics-legal | Ethics & Legal | 9 |
| neuroscience | Neuroscience Team | 1 |
| restricted/hr | HR team meeting, "1 to 1" topics (John to confirm) | 2 plus 1:1s |
| misc | n8ntest (7), "my meeting", "My Meeting", "Flo Labs's Zoom Meeting", one-offs | 9 plus one-offs |

**Open mapping questions for John**

1. Is "Robotics and Dev Meeting" the same team as "Robotics Team meeting"?
2. Should "Design Marketing & Data Analytics" stay separate from Design Team and Data Analytics?
3. Which team owns "Technical Meeting"?

## Phase 0: decisions and prerequisites

Phase 0 is done when every box below is ticked. Phases 1 to 3 can start while the decisions are open, but Phase 6 (the full backfill) cannot, because the team mapping decides the R2 paths.

**Decisions (John Hazlewood)**

- [ ] Approve the DIY plan and the use of his Owner account to create the Zoom app
- [ ] Answer the 3 open team-mapping questions in the R2 layout section
- [ ] Decide which meetings are restricted (HR, "1 to 1" topics, others) and so excluded from AI access
- [ ] Name who may query the archive through AI. Their emails use three domains: flomadlabs.com, flomad.com and flolabsinnovations.com
- [ ] Confirm the monthly budget ceiling (see Cost estimate)

**Prerequisites (implementer, with the owner named)**

- [ ] Rotate the Cloudflare credentials and the R2 test token that were pasted in chat (Cloudflare account owner)
- [ ] Confirm the R2 plan is active with a working payment method, and set a budget alert under Billing > Billable Usage (Cloudflare account owner)
- [ ] Create a new Hetzner project for this work, separate from other servers (implementer)
- [ ] Set up a team password manager vault for the Zoom and Cloudflare secrets (implementer)
- [ ] Book 30 minutes with John to create the Zoom app in Phase 2 (implementer)
- [ ] Find out who moved the 65 recordings to Trash, and when (ask the three Zoom users)
- [ ] Check each Zoom user's installed apps and remove RecordFlow if it is there (each user)
- [ ] Check the Zoom portal tabs not yet looked at: "Transcripts", "My Notes", and recordings older than 12 months (implementer)

**Time-critical item.** The 65 Trash recordings show 30 days left. Start Phase 2 and Phase 5 early enough that the Trash items are archived with weeks to spare. Do not recover or purge anything yourself without John's say-so. Recovering an item from Trash is not a deletion, but it adds to a Zoom account that is already over quota, and we do not know yet what Zoom does in that state.

## Phase 1: Cloudflare R2 setup

Output of this phase: one private bucket and one bucket-scoped key pair, stored in the team vault. Allow about 30 minutes. Do these steps in the Cloudflare dashboard; menu names can shift, so match by name, not by position.

1. Confirm the credentials from the earlier chat are rotated (Phase 0). Do not reuse any old key or token.
2. Go to R2 Object Storage and click Create bucket. Name it `flolabs-zoom-archive`. Leave the location on automatic unless company policy names a region.
3. Leave the bucket private. Do not enable the public r2.dev URL and do not attach a custom public domain.
4. Open Manage API tokens (R2 overview page) and create one token:
   - Name: zoom-sync-hetzner
   - Permission: Object Read & Write
   - Scope: this bucket only (flolabs-zoom-archive)
   - Optional: restrict by the Hetzner server's IP once it exists (Phase 3)
5. Copy the Access Key ID, Secret Access Key and Account ID straight into the vault. The secret is shown once. Never paste them into chat, a doc or a ticket.
6. Write down the S3 endpoint: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`, region auto.
7. Set a budget alert under Billing > Billable Usage (suggested trigger: USD 15 a month, about three times the expected cost).
8. Optional and unverified: check whether the account offers R2 bucket locks (retention rules). If it does, a retention rule on meetings/ protects the archive from accidental overwrite or delete.

**No token for the AI side.** The MCP server in Phase 8 runs as a Cloudflare Worker with the bucket attached as a binding. It needs no access key, and its code only calls read and list. That keeps the key pair above on the Hetzner server alone.

**Upload settings that matter.** R2 accepts the S3 API, but recent AWS SDK versions add checksum headers by default that R2 can reject. Set both variables below on the server before the first upload. Multipart rules to respect: every part except the last at least 5 MiB and all parts the same size, at most 10,000 parts, at most 5 GiB per part.

```
AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED
AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED
```

**Done when:** from your own laptop you can list the empty bucket with the new key pair, and an attempt with the old key fails.

## Phase 2: Zoom Server-to-Server app from John's account

John must do this himself, signed in as john@flomadlabs.com (Owner). A Server-to-Server app can hold no more privilege than the admin who creates it, and Zoom strips its scopes if that admin is later downgraded. So the app belongs under the Owner, and John's role must not change while the project runs. Allow 30 minutes with John.

1. Open the Zoom App Marketplace (marketplace.zoom.us), choose Develop, then Build App, then Server-to-Server OAuth App.
2. Name it FloLabs Archive Reader. Fill the required company and contact fields.
3. Copy the Account ID, Client ID and Client Secret into the team vault. Nobody types them into chat.
4. On the Scopes tab, add only the read scopes in the table below.
5. Leave event subscriptions (webhooks) off. The script polls the REST API, so no public endpoint is needed.
6. Activate the app. If Zoom blocks activation, it names the missing field.

| Scope | Used for | Status |
|---|---|---|
| user:read:list_users:admin | List the 3 users of the account | Named in two third-party integration guides |
| cloud_recording:read:list_user_recordings:admin | List each user's recordings, including Trash | Confirmed in a Zoom staff reply on the developer forum |
| cloud_recording:read:list_recording_files:admin | Get the file list and download links of one meeting | Name from earlier research. Confirm it in the scope picker |
| Any extra read scope the spike test asks for | Downloads or transcripts | Add only the scope named in the error message |

**Scope rule.** Never add a scope whose name contains delete, write or update. When a call fails with "does not contain scopes", Zoom's error text names the missing scope. Add exactly that one, and only if it is a read scope.

**How the app is used**

- Token: `POST https://zoom.us/oauth/token` with `grant_type=account_credentials` and the Account ID, authenticated with the Client ID and Secret. Tokens last 1 hour, so the script refreshes them.
- The `me` shortcut does not work with this app type. The script lists users first and then lists recordings per user ID.
- The recordings list accepts at most one month per request, so the script walks month by month.
- Trash is listed with `trash=true` and `trash_type=meeting_recordings`. It cannot be filtered by date.

**Done when:** the token request returns a token, and a call to list users returns all 3 users (john@flomadlabs.com, PM@flomad.com, zoom.buddy@flolabsinnovations.com). If it returns only John, the app does not see the whole account and we stop and rethink before building anything else.

## Phase 3: Hetzner server setup

One small Cloud server runs the backfill and then the scheduled sync. The script streams each file from Zoom straight into R2, so the server needs little disk. Allow about an hour.

| Setting | Choice | Why |
|---|---|---|
| Project | New project flolabs-zoom-archive | Keeps billing and access apart from other servers |
| Location | An EU location (Falkenstein, Nuremberg or Helsinki) | Larger traffic allowance than the US locations. Verify in the console |
| Plan | Smallest shared plan with 2 vCPU, 4 GB RAM, 40 GB disk or more | Parts are buffered in memory, 2 to 4 downloads at once. Price is roughly EUR 4 to 7 a month; sources disagree, so read it in the console |
| Image | Ubuntu 24.04 LTS | Long support window |
| Access | SSH key only. No password login | The server holds the Zoom and R2 secrets |
| Cloud firewall | Inbound: SSH (22) from the implementer's IP only. Everything else closed. Outbound: open | The server never serves anything |
| Protection | Turn on delete protection while the backfill runs | Stops an accidental server delete |

**Steps**

1. Create the server with your SSH public key and attach the firewall.
2. Log in as root once, create a normal user and harden SSH. Test the new login in a second terminal before you close the first one.

```
adduser archiver && usermod -aG sudo archiver
mkdir -p /home/archiver/.ssh && cp /root/.ssh/authorized_keys /home/archiver/.ssh/
chown -R archiver:archiver /home/archiver/.ssh && chmod 700 /home/archiver/.ssh
printf 'PermitRootLogin no\nPasswordAuthentication no\n' > /etc/ssh/sshd_config.d/99-hardening.conf
systemctl reload ssh
```

3. As archiver, update the system and install the tools.

```
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-venv python3-pip sqlite3 tmux unattended-upgrades
sudo mkdir -p /opt/zoom-archive /etc/zoom-archive /var/lib/zoom-archive /var/log/zoom-archive
sudo chown -R archiver:archiver /opt/zoom-archive /var/lib/zoom-archive /var/log/zoom-archive
python3 -m venv /opt/zoom-archive/venv
/opt/zoom-archive/venv/bin/pip install boto3 requests
```

4. Create the secrets file by typing the values from the vault. It is readable by archiver only and never leaves the server.

```
sudo touch /etc/zoom-archive/env
sudo chown archiver:archiver /etc/zoom-archive/env && sudo chmod 600 /etc/zoom-archive/env
nano /etc/zoom-archive/env      # variable names are in Appendix A
```

5. Check that the server clock is synced (`timedatectl` shows "System clock synchronized: yes"). Zoom tokens fail if the clock drifts.
6. Optional: edit the R2 token in Cloudflare to accept only this server's IP.

**Smoke test.** This must print a number and no error:

```
set -a; . /etc/zoom-archive/env; set +a
/opt/zoom-archive/venv/bin/python - <<'EOF'
import os, boto3
s3 = boto3.client("s3", region_name="auto",
    endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
print(s3.list_objects_v2(Bucket=os.environ["R2_BUCKET"], MaxKeys=5).get("KeyCount"))
EOF
```

**Done when:** root and password logins are refused, the smoke test prints 0, and the clock is synced.

## Phase 4: the sync script

One Python 3 program, zoom_archive, with three modes: backfill, sync and verify. It only reads from Zoom. The only non-GET call it makes to Zoom is the token request. Write it as small modules so each can be tested alone.

| Module | Job |
|---|---|
| zoom_client.py | Token cache (refresh a few minutes before the 1-hour expiry). One zoom_get() function. Retry on 429 and 5xx, honoring Retry-After. Functions: list users, list recordings (per user, per month, and Trash), get one meeting's recordings. Meeting UUIDs that start with / or contain // must be URL-encoded twice |
| r2_client.py | boto3 S3 client for the R2 endpoint. head_object, and multipart upload_fileobj with 64 MiB parts |
| naming.py | Topic slug, folder path, file names (see R2 layout). If the slug is empty, as with an Arabic-only topic, use meeting-<zoom_meeting_id>. The real topic stays in meeting.json |
| teams.py | Reads teams.yaml and maps a topic to a team slug. Unmatched goes to misc and into the report |
| manifest.py | SQLite state (below) |
| sync.py | The loop below |
| verify.py | The reconciliation report for Phase 6 |

**Command line**

- `--mode backfill | sync | verify`
- `--from YYYY-MM-DD --to YYYY-MM-DD` (default for backfill: 2020-01-01 to today)
- `--user EMAIL`, `--meeting-id ID` (narrow a test run)
- `--include-trash` (on by default in backfill)
- `--limit N`, `--concurrency 1..4`, `--dry-run` (list and plan, download nothing). Sync mode also takes `--lookback-days N` (default 7)

**The loop**

1. Get a token. List users with status active, inactive and pending.
2. Trash first. For each user, list Trash (trash=true, trash_type=meeting_recordings) and mark those rows source=trash. Trash has a 30-day clock, so it goes before history.
3. Then, for each user, walk the months from newest to oldest and list recordings (page_size=300, follow next_page_token).
4. For each meeting, skip it if every file is verified in the manifest. Otherwise call get-meeting-recordings to receive fresh download links.
5. Skip any file whose Zoom status is not completed (still processing). It is picked up next run.
6. Build the team, folder and file names. Check R2 with head_object: if the object exists with the same size as Zoom reports, mark verified and move on.
7. Otherwise stream the download into a multipart upload, hashing with sha256 on the way. Never write the video to local disk.
8. Verify: bytes received equal Zoom's file_size, and the R2 object size equals it too. Store both and the hash. Any mismatch is failed with the reason.
9. When all files of a meeting are verified, write its meeting.json.
10. At the end of the run, rebuild index/meetings.jsonl and .csv from the manifest, copy manifest.sqlite to state/ in R2, and print a summary: files done, skipped, failed, bytes, unmapped topics.

**Download authentication is the one open technical question.** Sources disagree on how a Server-to-Server token may fetch a download_url. The spike test in Phase 5 tries these in order and the code keeps the first that works:

- A: header `Authorization: Bearer <token>` on the download URL
- B: the token as `?access_token=<token>` on the download URL
- C: if both fail, a second Zoom app of the General (OAuth) type, authorized once by John. Only the download step changes

**Manifest (SQLite)**

```sql
CREATE TABLE files (
  zoom_file_id TEXT PRIMARY KEY, meeting_uuid TEXT, meeting_id INTEGER,
  host_email TEXT, topic TEXT, team TEXT, source TEXT,        -- active | trash
  recording_type TEXT, file_type TEXT, zoom_size INTEGER,
  r2_key TEXT, r2_size INTEGER, sha256 TEXT,
  status TEXT,                                                -- pending | uploaded | verified | failed
  attempts INTEGER DEFAULT 0, last_error TEXT, updated_at TEXT
);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT, started_at TEXT, finished_at TEXT,
  files_done INTEGER, files_failed INTEGER, bytes INTEGER);
```

**Errors**

| Case | Action |
|---|---|
| 401 | Refresh the token once, retry. Then fail the file |
| 429 | Wait for Retry-After, else exponential backoff. Lower concurrency by one |
| 5xx or network drop | Retry up to 5 times with exponential backoff. Abort the multipart upload before retrying |
| 404 or 410 on a download | Mark failed with the reason and continue. It is listed in the run report |
| Size mismatch | Mark failed, never verified |

**Safety checks built into the code**

- A test that fails the build if the code contains an HTTP DELETE, PUT, PATCH or any call to a Zoom path containing delete.
- A start-up check that decodes the token's granted scopes (or reads them from the token response) and refuses to run if any contains delete or write.
- The R2 key pair has no delete use in the code. Overwrites happen only for a file marked failed.

## Phase 5: tests before the full run

Run these four tests in order. Each one is bigger than the last, and the backfill starts only when all four pass. All of them run against Zoom read-only, so none can lose data.

| Test | What to run | Pass condition | If it fails |
|---|---|---|---|
| 1. Spike | One tiny recording, the n8ntest meeting (7 exist). List users, list that month, get the meeting's recordings, then try download methods A, B and C from Phase 4. Upload to _spike/ in R2 | The file in R2 has the exact byte size Zoom reports, and a video or VTT opens | Try the next download method. If all three fail, stop and send the error text to the project owner |
| 2. Trash spike | One recording from the 65 in Trash. Is it listed with trash=true, and does its download work? | Listed, downloaded, size equal | If only listing works, John approves recovering the items from Trash in the portal (Recover is not a deletion), then the script archives them |
| 3. Pilot | One team, one month: Design Team, November 2026. Run with --from 2026-11-01 --to 2026-11-30 | Meeting count and total size match the portal. The Nov 4 transcript.vtt opens and its first and last 10 lines match the transcript Claude fetched through the connector | Fix naming or listing, rerun. Reruns must upload nothing that is already verified |
| 4. Partial | One full month for all 3 users, one that includes Trash items (July 2026) | Counts and sizes match the portal. Press Ctrl-C mid-run, restart: it resumes. Run it twice: the second run uploads 0 files. Run with a wrong R2 key: files fail and none is marked verified | Fix before moving on |

**Record while testing**

- The real file_type and recording_type values, to correct the naming table in the R2 layout section.
- Which download method worked.
- Download speed in MB/s and the time per GB. Use it to refine the backfill estimate (see Cost estimate).
- Any rate-limit response from Zoom, with its headers.
- Meetings with several segments, to check the -2, -3 suffix rule.

**Note on the other hosts.** One forum thread reports that recordings of other hosts did not appear for a single-user app. This plan lists recordings per user ID with :admin scopes, which should include all three users, but test 4 must prove it: the portal shows recordings from three hosts, and all three must appear in the manifest.

**Done when:** all four tests pass and the results are written as comments on this section.

## Phase 6: full backfill and verification

The backfill copies about 325 GB. It is slow but safe to interrupt, because the script skips whatever is already verified. Count on one to two days; test 4 gives the real speed.

**Before you start**

- [ ] All Phase 5 tests passed
- [ ] Team mapping and restricted meetings are final (R2 has no rename)
- [ ] Delete protection is on for the Hetzner server
- [ ] The R2 budget alert is set

**Run it**

```
tmux new -s backfill
cd /opt/zoom-archive/app
set -a; . /etc/zoom-archive/env; set +a
/opt/zoom-archive/venv/bin/python -m zoom_archive --mode backfill --concurrency 3 2>&1 | tee -a /var/log/zoom-archive/backfill.log
```

Detach with Ctrl-B then D. If the server reboots or the run dies, start the same command again. Trash is processed first, then months from newest to oldest.

**Watch it once or twice a day**

```
sqlite3 /var/lib/zoom-archive/manifest.sqlite "select status, count(*), round(sum(zoom_size)/1e9,1) as gb from files group by status;"
```

Rerun until failed is 0. A file that keeps failing gets a written reason (for example "404 on download, Zoom says the file is gone") and John decides whether that is acceptable.

**Verification.** Run `--mode verify`. It fetches a fresh listing from Zoom and a fresh listing from R2, and does not trust the manifest alone. All checks must pass before Phase 9.

| Check | How | Pass |
|---|---|---|
| Meeting count | Per user and per month: Zoom listing vs manifest vs R2 folders. Compare the totals with the portal (961 in 12 months, 65 in Trash, plus older months) | No row differs |
| File sizes | Every file: Zoom file_size vs R2 object size | All equal |
| Bucket totals | R2 Metrics (object count, stored GB) vs manifest sums, within 1% | Within 1% |
| Nothing missing | Meetings in the fresh Zoom listing that are not in the manifest | 0 |
| Failures | Files with status failed | 0, or each one accepted in writing |
| Naming | Unmapped topics list and the misc folder reviewed by John | Reviewed |
| Spot check | 20 random meetings, at least 2 per team, one with Arabic text: open the transcript, play 30 seconds of video, read the chat | All 20 good |
| Known gaps | Local recordings, recordings of deleted users, AI Companion summaries, My Notes: each has a decision (covered elsewhere, accepted loss, or follow-up task) | Decided |

The verify mode writes `reports/verification-<date>.md` to R2. Paste its summary into this section and ask John to review it.

**Done when:** the table above is all green and the report is in R2.

## Phase 7: ongoing sync

After the backfill, the same script runs once a day and copies new meetings. Polling is simpler than webhooks here, because there is no public endpoint to run and secure. Zoom finishes transcripts and processing some time after a meeting ends, so each run looks back 7 days. Files still marked as processing are skipped and picked up the next day.

Add a `--lookback-days N` option to sync mode (default 7). A scheduled run does this:

1. Lists all users, since new people may join the Zoom account.
2. Lists recordings of the last N days, plus the whole Trash list.
3. Archives anything not yet verified, then rebuilds the index and copies the manifest to R2.
4. Exits with a non-zero code if any file failed.

```
# /etc/systemd/system/zoom-archive-sync.service
[Unit]
Description=FloLabs Zoom to R2 sync

[Service]
Type=oneshot
User=archiver
WorkingDirectory=/opt/zoom-archive/app
EnvironmentFile=/etc/zoom-archive/env
ExecStart=/opt/zoom-archive/venv/bin/python -m zoom_archive --mode sync --lookback-days 7

# /etc/systemd/system/zoom-archive-sync.timer
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it with `sudo systemctl daemon-reload && sudo systemctl enable --now zoom-archive-sync.timer`. The server clock is UTC, so 03:30 is 05:30 in Cairo in winter. Check with `systemctl list-timers`.

**Keep it healthy**

- **Failure alerts.** A failed run marks the systemd unit as failed. Add a notification of the team's choice (email through a mail relay, or a message to a chat webhook). Without it, a broken sync is invisible.
- **Weekly verify.** Run `--mode verify` once a week from a second timer, and read the report for any Zoom-vs-R2 difference.
- **Monthly human check.** Compare the Zoom portal's recording count for last month with the index. Five minutes.
- **Secret rotation.** Put a reminder every 6 months to rotate the Zoom client secret (John, in the Marketplace) and the R2 key pair (Cloudflare owner), then update /etc/zoom-archive/env.
- **Code changes.** Keep the script in a private git repository and deploy with git pull. The script version is written into every meeting.json.

The server stays after the backfill, because the daily sync needs somewhere to run. If the cost matters, the same job can move to a scheduled Cloudflare Worker later, but that is a separate project and is not planned here.

## Phase 8: connect Claude and ChatGPT

Both tools talk to one small read-only MCP server that we build on a Cloudflare Worker and point at the bucket. Claude and ChatGPT then call its tools to list meetings, read transcripts and search them. This phase can start once Phase 5 test 3 has put real meetings in R2. Users get access after Phase 6 passes.

**What we build.** A Worker named `flolabs-meetings-mcp`, written with Cloudflare's Agents SDK (McpAgent), serving MCP over streamable HTTP at `/mcp`. The bucket is attached as a Worker binding, so the Worker needs no R2 key, and its code only reads and lists. Do not use Cloudflare's own account-wide MCP server for this. It can manage the whole account and is far too broad.

| Tool | What it returns |
|---|---|
| list_meetings | Meetings filtered by team, date range and topic text. One line each: key, topic, team, date, duration |
| get_meeting | The meeting.json of one meeting (no signed links) |
| get_transcript | Timestamped, speaker-labeled text from transcript.vtt, in pages of at most about 30,000 characters with a cursor. A one-hour meeting is too long for one reply |
| search_transcripts | Matching passages with meeting key and timestamp. Needs the search index (v2) |
| search and fetch | Aliases with the plain search and fetch names, because one source says ChatGPT's retrieval features expect read-only tools with those names. Unverified, test it |

No tool writes, copies or deletes. A tool that returns a time-limited link to a video file is left out of v1, because it needs a read key inside the Worker.

**Who can log in.** The server is never open to the internet without login. Decide between two ways, and prove the choice early with a dummy ping tool that returns no data:

- Cloudflare Access in front of the Worker. Cloudflare documents this for MCP servers. Sign-in by email code or Google, with an allowlist of the people John names. Least code. Confirm that both Claude and ChatGPT can finish the sign-in.
- workers-oauth-provider with Google sign-in and an email allowlist in code. Cloudflare's example template uses GitHub; Google needs a small swap. More code, more control.

**Restricted meetings.** The Worker must never read HR and 1:1 recordings. The safe way is a second bucket for them with no binding to this Worker, so a coding mistake cannot expose them. If the team keeps a restricted/ prefix in one bucket instead, the code must hard-block that prefix and a test must prove it. Decide before Phase 6.

**Search, in two steps**

- v1, no index. list_meetings reads index/meetings.jsonl, and the AI finds the meeting by team and date, then reads its transcript. This already answers "what did the Design Team decide on Nov 4?"
- v2, full-text search. After each sync, a job on the Hetzner server splits each transcript into passages of about a minute and loads them into a Cloudflare D1 table with an FTS5 index. D1 supports FTS5. Two things must be tested with real data: how well FTS5 handles Arabic (the default tokenizer may split Arabic poorly; the trigram tokenizer is the fallback), and whether the total transcript size fits the D1 database size limit. The job needs a Cloudflare token limited to editing this one database.
- Cloudflare AI Search is an alternative. It has a 4 MB per-file limit and we have not confirmed that it reads VTT files, so it is not the plan.

**Connect the clients**

- **Claude, Team or Enterprise.** An Owner opens Organization settings > Connectors, chooses Add, then Custom, then Web, and enters `https://<worker>.<account>.workers.dev/mcp`. Each member then clicks Connect and signs in. On Pro and Max, use Customize > Connectors > Add custom connector. Free allows one custom connector. Claude calls the server from Anthropic's cloud, so the Worker must be reachable from the public internet, protected by the login above.
- **ChatGPT.** Needs Developer mode. On Business, Enterprise and Edu a workspace admin turns it on first (Workspace Settings > Permissions & Roles > Connected Data). Then Settings > Apps & Connectors > Create, with the same /mcp URL. Plans differ between sources: one says full custom MCP is Business, Enterprise and Edu only and Plus and Pro get read-only connectors, another lists Plus and Pro in general. Check what the company's ChatGPT plan allows before promising this to the team.
- After any tool change, refresh the connector in both apps. Tool lists are cached.

**Acceptance tests**

- "List the Design Team meetings of November 2026." The Nov 4 meeting is in the answer.
- "What did we decide in the Design Team meeting on Nov 4, 2026?" The answer matches the transcript.
- A question in Arabic about a meeting held in Arabic.
- "What was said in the HR team meeting?" The answer is that nothing was found.
- A person who is not on the allowlist cannot sign in.

Before deploying, run SkillSpector on the Worker code. The company rule is to stop if the risk score is above 50. Transcripts sent into Claude or ChatGPT answers leave Cloudflare, so the plan tier (data use and retention terms) of each tool is a company decision.

## Phase 9: retention gate

This gate decides when John may even consider deleting anything from Zoom. Passing it deletes nothing. It only lets John decide, in writing, what may be removed and by whom. Until every box is ticked, Zoom keeps all recordings, transcripts and Trash items exactly as they are.

- [ ] The Phase 6 verification table is all green and its report is stored in R2
- [ ] At least two weeks of daily sync have run since the backfill, with clean weekly verify reports, so we know new meetings arrive
- [ ] A restore test was done by someone other than the implementer: download 5 random meetings from R2 to a laptop and play the video, read the transcript and the chat
- [ ] The Phase 8 acceptance tests passed, so the transcripts are usable by the AI tools and not only stored
- [ ] Known gaps (local recordings, deleted users, AI Companion summaries, My Notes) each have a recorded decision
- [ ] R2 billing is healthy: valid payment method, budget alert set, and nobody expects a failed payment. A payment that stays failed can lead to data deletion after 30 days
- [ ] A second copy of the archive exists on another provider, or John has accepted in writing that R2 will be the only copy. Recommended: a second copy
- [ ] John Hazlewood approved in writing, naming exactly what may be deleted (for example "recordings older than a stated date, excluding Trash") and who will do it

**What happens after the gate (not part of this project)**

- Deleting is done by people in the Zoom portal, not by the script and not by an app.
- Start with a small batch, then wait. Zoom's Trash keeps deleted recordings for 30 days, which is the only undo, so the first batch is a test.
- Whether deleting frees quota immediately or only after the Trash is purged is not confirmed. Check on the first batch.

**While the account is over quota.** What Zoom does at 141% of the quota is unknown. It may stop new recordings. If that happens before the gate passes, the safe answer is more storage, not deletion. Ask John to check the price of extra cloud storage so a decision is ready.

## Security and access control

The archive holds every internal meeting of the company, so each key gets the smallest power that works and a named owner.

| Secret or access | Lives in | Held by | Rotate |
|---|---|---|---|
| Zoom app Account ID, Client ID, Client Secret (read scopes only) | Team vault and /etc/zoom-archive/env (mode 600) | John (creator), implementer | Every 6 months, or when someone with access leaves |
| R2 key pair, Object Read & Write, this bucket only | Team vault and the same env file | Cloudflare owner, implementer | Every 6 months, and now, because earlier credentials were pasted in chat |
| D1 token for search loading (v2 only), limited to one database | Env file | Implementer | Every 6 months |
| Hetzner server | SSH key login only, firewall to the implementer's IP | Implementer and one backup person | When people change |
| Cloudflare dashboard | Accounts with 2-step login on | Named people only | Review quarterly |
| MCP login allowlist | Cloudflare Access policy or Worker config | John approves each name | Review monthly, remove leavers the same day |

**Rules**

- No secret is ever typed into chat, a doc, a ticket, a commit or a log. The incident that already happened is the reason for this rule.
- The script must not log tokens or full download URLs. Some Zoom download links carry an access token in the address.
- The Worker has no write path to R2 and no binding to the restricted bucket.
- Remove the RecordFlow authorization from each Zoom user (Phase 0). Its optional scope can delete recordings.
- Keep the Zoom app's scopes read-only. A new scope needs John's approval and must not contain delete, write or update.

**Transcripts are untrusted text.** Anyone in a meeting, including a guest, can say a sentence that looks like an instruction to an AI tool. Our tools are read-only, so the worst result is a wrong answer, but a user who has other connectors switched on (email, files) in the same chat is at more risk. The get_transcript tool should label its output as meeting content, and users should keep sensitive connectors off when reading old transcripts from outside guests.

**Privacy.** Recordings contain names and voices of staff and possibly outside guests. The Ethics & Legal team should confirm retention and consent rules before the AI access goes live. This document is not legal advice.

**If a secret leaks:** revoke the R2 key pair and the Zoom client secret first, then create new ones, update the env file, and check the Cloudflare and Zoom activity logs for use you do not recognize.

## Cost estimate

Expect about USD 9 to 13 a month once everything runs, and under EUR 1 of server time for the backfill itself. Storage is the main cost, and it grows with every meeting recorded after today.

| Item | Basis | Per month |
|---|---|---|
| R2 storage | 325 GB, minus the 10 GB free, at USD 0.015 per GB-month | about USD 4.70 |
| R2 operations and downloads | The backfill makes about 5,000 part uploads (325 GB in 64 MiB parts), far under the 1 million free monthly writes. Downloads out of R2 cost nothing | USD 0 |
| Hetzner server | Smallest shared plan, hourly billing with a monthly cap. Sources disagree, so read the price in the console | EUR 4 to 7 |
| Cloudflare Worker, D1 and Access | Free tiers are probably enough. If the Workers Paid plan is needed, it is about USD 5. Not verified, so check the limits | USD 0 to 5 |
| Zoom extra storage | Only if Zoom blocks new recordings while over quota. Price unknown | Ask John to look up |

**Billing details to know.** R2 bills at the end of each 30-day period, so the first invoice arrives after the first period closes (for this account probably around Dec 7, 2026; an inference from the billing screen). Storage is averaged per day. The card must work then, because a payment that keeps failing leads to errors from R2 after a 5-day grace period and, after 30 days without a fix, possible data deletion.

**Backfill time.** The slow part is Zoom's download speed, not the server. For 325 GB: at 50 MB/s about 2 hours, at 10 MB/s about 9 hours, at 5 MB/s about 18 hours, at 2 MB/s about 45 hours. Test 4 in Phase 5 measures the real figure. Plan for two days to be safe.

## Risks, open questions and decisions

The three that can change the plan are the first three rows. Each has a test in Phase 5 that answers it before the big run.

| Risk or open question | Why it matters | Mitigation |
|---|---|---|
| A Server-to-Server token may not be allowed to download files | Sources disagree. If it fails, the script design changes at one step | Phase 5 test 1 tries methods A, B and C. Fallback: a second General OAuth app authorized by John |
| Trash items may not be downloadable through the API | 65 recordings have about 30 days left | Phase 5 test 2. Fallback: John approves recovering them in the portal, then the script archives them |
| The app may not list recordings of all three users | The plan assumes one Owner-created app covers all users. A forum report says other hosts' recordings did not appear for a single-user setup | Phase 2 must list 3 users. Phase 5 test 4 and the Phase 6 count check prove it |
| Zoom's behavior at 141% of quota is unknown | New recordings might be blocked before the archive is ready | Ask John for the price of extra storage. Never fix this by deleting |
| Not covered by the cloud recording API: local recordings, recordings of deleted users, AI Companion summaries, My Notes | The archive would look complete but miss these | Each gets a decision in Phase 6. Check the portal tabs not yet viewed |
| R2 payment fails | Errors after 5 days, possible data deletion after 30 days | Valid card, budget alert, a second billing contact |
| R2 becomes the only copy once Zoom is cleaned up | One provider, one mistake | A second copy before Phase 9 is recommended |
| Wrong team names in paths | R2 has no rename. Fixing means copying every object | Final mapping before Phase 6. teams.yaml plus the unmapped report |
| ChatGPT may not allow custom connectors on the company plan | One of the two AI tools would not work | Check the plan first. Claude works regardless |
| Arabic search quality in D1 FTS5 | Arabic transcripts could return poor matches | Test with real transcripts in v2. Fallback: the trigram tokenizer |
| Zoom's own example docs may be stale | Zoom's published recording-download example names a scope (recording:read) that looks outdated and shows S3 and Google Cloud, not R2 | Trust the live API responses and the scope picker over any example |

**Decisions still open**

- [ ] MCP login: Cloudflare Access or workers-oauth-provider with Google (owner: implementer proposes, John approves)
- [ ] Restricted meetings: second bucket (recommended) or a prefix in one bucket
- [ ] Second backup copy: yes or no, and where
- [ ] Who may query the archive through AI (list of emails)
- [ ] Extra Zoom storage: price and whether to buy it as a stopgap
- [ ] The three team-mapping questions in the R2 layout section

## Appendices

### A. Environment file template

Names only. Values come from the vault and are typed on the server. R2_RESTRICTED_BUCKET exists only if the team chooses a second bucket for restricted meetings.

```
ZOOM_ACCOUNT_ID=
ZOOM_CLIENT_ID=
ZOOM_CLIENT_SECRET=
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=flolabs-zoom-archive
R2_RESTRICTED_BUCKET=
AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED
AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED
```

### B. Zoom endpoints the script uses

| Call | Purpose |
|---|---|
| `POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=<ACCOUNT_ID>` with the Client ID and Secret as basic auth | Get a 1-hour token |
| `GET https://api.zoom.us/v2/users?status=active&page_size=300` (repeat for inactive, pending) | List users |
| `GET https://api.zoom.us/v2/users/<userId>/recordings?from=<date>&to=<date>&page_size=300` | One user's recordings for up to one month, paged with next_page_token |
| Same path with `trash=true&trash_type=meeting_recordings`, no dates | One user's Trash |
| `GET https://api.zoom.us/v2/meetings/<meetingId or UUID>/recordings` | Files and fresh download links of one meeting. Double-encode a UUID that starts with / or contains // |
| `GET <download_url>` with method A, B or C from Phase 4 | The file itself |

No other Zoom call is made. Check the exact parameter names against the live API reference when writing the client.

### C. Pseudocode

```
token = zoom.get_token()                       # refresh before it is 55 minutes old
users = zoom.list_users(statuses=["active", "inactive", "pending"])

meetings = []
for u in users:                                # Trash first: it has a 30-day clock
    meetings += zoom.list_trash(u.id)          # source = "trash"
for u in users:
    for month in months_newest_to_oldest(start, end):
        meetings += zoom.list_recordings(u.id, month.first, month.last)   # source = "active"

for m in meeti
```

*(The pasted PRD was cut off here.)*
