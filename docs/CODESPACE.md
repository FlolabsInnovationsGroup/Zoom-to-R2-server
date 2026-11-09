# Running the copy in a GitHub Codespace

A Codespace sits in a datacenter, so the Zoom to R2 copy runs much faster there than on a home connection.
This is a one-off for the big first copy. The archive's real home is still the Hetzner server (see DEPLOY.md).

Run **one copy at a time**: stop the copy on the laptop before starting it in the Codespace, and the other way round.

## 1. Before you open the Codespace

1. **Push the code.** The Codespace is built from GitHub.
2. **Raise the idle timeout.** Go to github.com/settings/codespaces and set *Default idle timeout* to 240 minutes.
   Otherwise the Codespace shuts down after 30 minutes without typing and the copy stops. If that happens anyway,
   press Start again: it carries on where it stopped.
3. **Add the secrets.** Go to github.com/settings/codespaces, then *Codespaces secrets*, then *New secret*.
   Give each secret access to the repository `Ahsan-Yasin/Meetings-server`. Use the same values as in your `.env`:

   | Secret | What it is |
   | --- | --- |
   | `ZOOM_ACCOUNT_ID` | Zoom Server-to-Server OAuth app |
   | `ZOOM_CLIENT_ID` | Zoom Server-to-Server OAuth app |
   | `ZOOM_CLIENT_SECRET` | Zoom Server-to-Server OAuth app |
   | `R2_ACCOUNT_ID` | Cloudflare account ID |
   | `R2_ACCESS_KEY_ID` | R2 token for bucket `zoom-archive-test` |
   | `R2_SECRET_ACCESS_KEY` | R2 token for bucket `zoom-archive-test` |
   | `ADMIN_PASSWORD` | The admin page password |
   | `ADMIN_SECRET_KEY` | Signs the admin login cookie |

   Never put these values in a file in the repository, in a commit, or in a Copilot chat.
4. **Stop the laptop copy.** On the laptop's admin page (http://127.0.0.1:8080/admin), press **Stop** and wait
   until it says *Paused*.
5. **Keep the progress files ready.** These are `manifest.sqlite` and `search.sqlite` in the laptop's `.local` folder.
   They record what is already copied, so the Codespace skips those files and does not start again from zero.

## 2. In the Codespace

1. On GitHub, open the repository, then *Code*, then *Codespaces*, then *Create codespace on zoom-archive-v1*.
   2-core is enough: the work is mostly network.
2. In the file explorer, create a `.local` folder at the top of the repository. Drag `manifest.sqlite` and
   `search.sqlite` from the laptop into it. `.local` is in `.gitignore`, so they are never committed.
3. Paste the Copilot prompt below into Copilot Chat (Agent mode). It installs the app and starts the admin page.
4. Open the **Ports** tab and the address for port 8080. Leave the port **Private**: only your GitHub
   login can open it. Never make it Public.
5. Log in with the admin password and press **Start / Resume copying**. Keep the browser tab open.

## 3. When the copy is done

1. On the Codespace admin page, make sure it says *Done* and check the *Failed* count.
   If some failed, press Start again once: it retries only those.
2. In the Codespace file explorer, right-click `.local/manifest.sqlite`, choose *Download*, and do the same for
   `.local/search.sqlite`. Put both in the laptop's `.local` folder, replacing the old ones.
   Later they go to the Hetzner server.
3. Delete the Codespace (github.com/codespaces, then the menu on the Codespace, then *Delete*), so the meeting list and
   secrets do not stay on it. After that, delete the Codespaces secrets too, or keep them if you'll need them again.

## Copilot prompt

```text
This repository is a Python app: a Zoom to Cloudflare R2 archiver with an admin web page. Set it up and start the admin
page in this Codespace. Do not start the copy itself, I will press Start on the admin page.

Rules:
- Never print, echo, log or write the values of these environment variables, and never create a .env file:
  ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
  ADMIN_PASSWORD, ADMIN_SECRET_KEY. They are Codespaces secrets and are already in the environment.
  To check one is set, only print set or MISSING.
- Do not change any code or commit anything. Do not delete anything in R2. Do not run the tests.
- Do not touch .local/manifest.sqlite or .local/search.sqlite except to check they exist.

Steps:
1. Check the 8 variables above are set, printing only the name and set or MISSING for each. If any is MISSING,
   stop and tell me which one.
2. Check python3 --version is 3.12 or newer. If not, tell me and stop.
3. Check .local/manifest.sqlite exists and print its size. If it is missing, stop and tell me to upload it.
4. Create a virtual env and install the app: cd sync && python3 -m venv .venv && .venv/bin/pip install -q -e .
5. Run the setup check, which only reads from Zoom and R2:
   cd sync && .venv/bin/python -m zoom_archive --mode check
   Show me its output. If it fails, show the error and stop.
6. Start the admin page in the background, so it keeps running when this chat ends:
   cd sync && nohup env ZOOM_USER_EMAILS="john@flomadlabs.com,PM@flomad.com,zoom.buddy@flolabsinnovations.com" \
     R2_BUCKET=zoom-archive-test SYNC_CONCURRENCY=8 \
     AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED \
     PYTHONIOENCODING=utf-8 ADMIN_BIND=127.0.0.1:8080 \
     .venv/bin/python -m zoom_archive.web > ../.local/web.log 2>&1 &
7. Wait 5 seconds, then run: curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/admin
   It should print 303 or 200. If not, show me the last 30 lines of .local/web.log.
8. Make sure port 8080 is forwarded with visibility Private (not Public). Tell me the forwarded address
   to open in my browser.
```
