# TenderDesk

## Local accounts and recommendations

Sign in with a provisioned account. Eight-hour HS256 JWT sessions use an HttpOnly, SameSite=Strict cookie, a randomly generated persistent signing key, expiry checks and server-side logout revocation. Passwords use salted PBKDF2-SHA256 (600,000 iterations). Ten failed login attempts trigger a 15-minute local lockout. All data APIs, exports and PDF downloads require login. The cookie intentionally lacks Secure for this HTTP localhost deployment; do not expose this server publicly. Protect the database on disk. This is not a production identity service: no password-reset/email-verification flow or administrative roles are provided.

On Overview, expand **Edit your domains & interests**. Select multiple domains and/or comma-separated custom keywords. Recommendations match title, reference, organisation, category or description, exclude passed/unknown deadlines and corrigenda, and show matching terms. Matches are ordered by closing date, not ML relevance. Coverage is limited to the collected archive. Preferences and watchlists are user-specific; tender data, PDF cache and collector settings are shared. Legacy pre-login bookmarks remain preserved in the database but are not assigned to any new account.

Provision another local account (prompts securely for its password):

```powershell
python create_user.py --email user@example.com --name "Your name" --domain railway
```

Add `--generate-password` to generate and display a random password once. Accounts are not created automatically at application startup.

## PDF downloads and rapid refresh

Open a notice and choose **Find & download PDFs**. Public PDFs are fetched on demand, validated by their PDF signature, and saved in SQLite. Download links serve the saved file directly with an attachment header; documents are limited to 25 MB each. Cached copies show their retrieval date and are not automatically replaced. Confirm later revisions on the official portal. GeM was live-tested successfully; sampled GePNIC document endpoints returned HTML instead of PDFs, so those documents remain unavailable without the portal's interactive flow. CAPTCHA and login are not bypassed.

The background collector targets a start every 30 seconds and the dashboard refreshes every 10 seconds. Slow cycles do not overlap. Failed feeds back off for 1–30 minutes. This does not expand the existing limited feed coverage or guarantee that upstream portals publish in real time. The Windows background task must remain running and the computer must be awake and online.

A local procurement dashboard with real public records from GeM, CPPP (eProcurement + ePublishing), Defproc, and Hindustan Shipyard. Python 3.11+ only; no pip or npm dependencies.

## Start

Run `python app.py` in this directory, then open http://127.0.0.1:8765 . On Windows you can also run `powershell -ExecutionPolicy Bypass -File start.ps1`.

The process starts a collection immediately and targets a collection start every 30 seconds by default (slow cycles do not overlap). Closing the browser does not stop collection. Keep the server running; a sleeping/offline computer cannot collect. `install-autostart.ps1` registers a per-user Windows logon task; it is an optional OS-level setup separate from the server's own scheduler. Stop a foreground server with Ctrl+C. Run `Stop-ScheduledTask -TaskName TenderDeskCollector` to stop a task-launched server. Remove its logon task with `Unregister-ScheduledTask -TaskName TenderDeskCollector -Confirm:$false` if no longer needed.

Open Collection settings to adjust the interval (30–86400 seconds), number of latest GeM pages (1–20), and rolling backfill pages (0–10). A manual sync queues the next cycle. Source failures are isolated and recorded. Startup recovers interrupted source states.

## Features

- Responsive dashboard, notice search, source/type/deadline filters, sorting and pagination.
- Table/card views, persistent watchlist, notice drawer, revision history and official links.
- CSV export of matching records (maximum 10,000), formula-injection escaping.
- SQLite WAL persistence of notices, events, settings, bookmarks and collection runs.
- Source health, explicit coverage notes, retry/backoff, collection timestamps, stale-data errors.
- Latest GeM listings plus a persisted rolling page cursor. Public session token is fetched dynamically and is not hardcoded.
- Existing verified probe samples are imported once on an empty database, with import events; they are not marked as a successful live collection.

## Files and storage

`app.py`: HTTP server, collectors, scheduler and storage. `web/`: buildless UI. `data/tenders.sqlite3`: application database. `data/app.log`: collector errors and run summaries. Set `TENDER_DATA_DIR` to change storage; set `PORT` to change the local port. Back up SQLite using its backup API, or stop the server before copying the database and WAL files.

The interface uses the navy/paper/gold design direction of the Vizag Chambers project and compact rounded panels inspired by Moto-Stock. Google fonts are optional; system fonts are the fallback.

## Coverage and semantics

This is an operational local MVP, not a complete mirror of the portals. GePNIC homepages expose limited recent windows (typically 10 tenders + 10 corrigenda). HSL uses `eprocurehsl.nic.in`. Records can be missed between polls or while offline. Full active-tender traversal, awards, cancellations, generic announcements, email delivery and all document downloads are not implemented. GeM's listing request is an undocumented website endpoint and can change.

GePNIC records use source + reference + notice type (and corrigendum title) for identity. Repeated corrigenda with exactly the same reference and title become revisions; the homepage lacks a stable unique amendment identifier. CPPP's two feeds remain separate. Session URL changes do not create false revision events. Data is not deleted when it disappears from a feed. Deadline-derived Open/Closed is not an official awarded/cancelled status. UI counters represent stored records, not total government tenders.

Dates display in IST. GeM's public listing JavaScript displays the literal UTC-looking clock fields with UTC formatting, so the collector interprets that displayed clock as IST. Always verify final deadlines on the official page. Public GePNIC homepage notice links omit the collector-only `session` parameter so the official portal can initialize a fresh browser session. Stored notice and revision links are repaired on startup. Document-download links and other session-bound forms are not rewritten.

## Vercel + Supabase deployment

The repository now includes a Vercel Python Function (`api/index.py`) and `vercel.json`. It serves the existing frontend, verifies Supabase Auth access tokens, stores TenderDesk data in the isolated `tenderdesk_*` PostgreSQL tables, stores PDFs in the private `tenderdesk-documents` bucket, and invokes the stateless collector every 15 minutes with Vercel Cron.

Before deployment, create the following Vercel environment variables for Production, Preview and Development. Copy the first two from Supabase **Connect**; generate a random `CRON_SECRET`. Keep the secret key server-only and never commit a `.env` file.

```text
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
SUPABASE_SECRET_KEY=sb_secret_...
CRON_SECRET=<long-random-value>
```

The schema migration has been applied to the connected Supabase project. After committing and pushing these files, import the repository in Vercel (or redeploy its connected project). Create a Supabase Auth user through the app's **Create account** button, configure its Auth URL settings to include the Vercel URL, then sign in and wait for the first Cron run.

## Tests

Run `python -m unittest discover -s tests -v` and `node --check web/app.js`. Tests use temporary databases and saved public fixtures; no network requests are required.
