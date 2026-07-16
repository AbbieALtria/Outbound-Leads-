# SEO Leads App

Local dashboard for pulling, ranking, and working HVAC/home-services SEO leads
from the Outscraper (Google Maps) API.

## Deploy to Railway (hosted, accessible anywhere)

The app is deploy-ready: `Procfile` runs gunicorn, it binds `$PORT`, the DB path
is configurable, and an optional password gate protects it.

1. **Push to GitHub** (private repo — it's a business tool):
   ```
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. **Create the Railway project:** New Project → Deploy from GitHub repo → pick
   this repo. Railway auto-detects Python + the Procfile and runs gunicorn.
3. **Add a persistent Volume** (⚠️ required — without it your database is wiped on
   every redeploy): in the service, add a Volume mounted at **`/data`**.
4. **Set environment variables** (service → Variables):
   - `DATA_DIR=/data` — puts `leads.db` on the persistent volume
   - `OUTSCRAPER_API_KEY=<your key>`
   - `APP_PASSWORD=<a strong password>` — **without this the site is wide open** to
     anyone with the URL, including the credit-spending "Pull New Leads" button
   - (later) `APIFY_TOKEN=<token>` for the Apify bulk-pull path
5. Open the Railway URL. The browser will ask for a login — use **any username**
   and the `APP_PASSWORD` you set.

**Data continuity:** the hosted app starts with an *empty* database — your local
`leads.db` (leads, DNC list, dedupe history) is intentionally not committed. To
carry it over, copy your local `leads.db` into the Railway `/data` volume (via the
Railway CLI). Otherwise the hosted instance builds its own history from scratch.

**Local vs hosted:** running locally still needs no login (no `APP_PASSWORD` set)
and stores `leads.db` next to the code. The two instances have separate databases
unless you deliberately sync them.

## Run it (easy way)

**Double-click `Start SEO Leads.bat`.** It starts the app and opens your web
browser automatically. Leave the black window open while you work; **close that
window to stop the app.** To start it again later, double-click the same file.

The first time, it installs the two packages the app needs (Flask and
requests) — that only happens once and needs an internet connection. If you
ever see "Python was not found," install Python from
https://www.python.org/downloads/ and tick **"Add Python to PATH"** during
setup, then double-click the file again.

Tip: right-click `Start SEO Leads.bat` → *Send to* → *Desktop (create shortcut)*
for a one-click icon on your desktop.

## Run it (manual way)

```
python app.py
```

then open http://localhost:5000

## One-time setup

Put your Outscraper key in the [.env](.env) file
(`OUTSCRAPER_API_KEY=your_key`) or set it as an environment variable.
The key is only read at pull time and never stored in the database.

## What's where

| File | Purpose |
|---|---|
| [app.py](app.py) | Flask web app (dashboard, history, settings, pull button) |
| [pipeline.py](pipeline.py) | Pull/filter/score logic. Also runs standalone: `python pipeline.py --industry hvac --leads 100` |
| [db.py](db.py) | Schema + one-time migration from the old files |
| `leads.db` | The single SQLite database — all leads, cities, industries, chain exclusions, settings, and pull history |

## Pages

- **Dashboard** — shows only the **current pull** (the most recent batch of
  leads), ranked by SEO-opportunity score, with a header naming the industry
  and time and a link to view everything in History. Click
  Called / Int. / No / Callb. on a row to record the call outcome (click again
  to undo); notes save on Enter or when you click away. Pick an industry and
  "Pull New Leads" works through cities in the background with live progress,
  stopping the moment the count is reached.
- **History** — every lead ever pulled, with an **Industry** column and
  filters for date / status / city / **industry** / search. "Callbacks due"
  shows everything marked callback. Each lead is tagged with the pull it came
  from, so older pulls stay here and out of the dashboard.
- **Import** — upload the raw call-log CSV from your dialer (needs a column
  with "phone" in the header and a `status_name` outcome column). Outcomes
  update automatically: VOICEMAIL/NO ANSWER → called, NOT INTERESTED → not
  interested, DO NOT CALL → dnc. DNC numbers are blocked from every future
  pull and left out of CSV exports; statuses you set by hand are never
  downgraded by an import.
## Campaigns (the app is not SEO-only)

The lead engine is **campaign-agnostic** — the same pull/filter/dedupe/validate/
DNC/export pipeline serves any offer. A **campaign** only decides *which signals
make a lead "hot"* and *the call-hook wording*. Pick the active campaign from the
dashboard dropdown; switching it **re-scores and re-ranks every lead instantly**,
no re-pull.

Presets included (all B2B, "close on call"):
- **SEO / Rank Higher** — scores no-website + weak reviews (runs a live site check).
- **Listing Verification / GBP Claiming** — scores **unclaimed** Google listings (`verified=false`), the strongest signal for a "verify your listing" pitch.
- **Reputation Management** — scores low rating + few reviews.
- **Website Design / Build** — scores no-website (runs a live site check).

In **Settings → Campaigns** you can add your own: give it a name, pick a preset to
copy its scoring, and set **audience (B2B / B2C)** and **goal (close on call /
appointment setting)**. Appointment-setting campaigns add an "Appt" outcome button
on each lead. Every lead stores neutral signals (no-website, reviews, rating,
unclaimed, no-email, phone type) so any campaign can score off them.

> **B2B vs B2C matters for compliance.** B2B business numbers are treated more
> leniently under the National DNC Registry; B2C consumer numbers are fully
> subject to it plus TCPA/state lists. The audience flag is there to keep that
> distinction explicit — always DNC-scrub before dialing.

**Planned data sources & tools** (only Outscraper is wired in today):

- *Discovery:* Outscraper (Maps signals — in use). Other Maps scrapers (Apify, SerpApi, Google Places) return the same data.
- *B2B contact enrichment:* [Apollo.io B2B Data](https://www.apollo.io/product/b2b-data) (cheap, LinkedIn-based), Data Axle / Salesgenie (best SMB owner coverage, pricey), DataLane (licensing-board/permit data, best for local trades). Enrich has-website leads only.
- *DNC / TCPA scrub* (highest-priority add for cold calling): [DNCScrub](https://www.dnc.com/dncscrub/), [Blacklist Alliance](https://www.blacklistalliance.com/blog/dnc-list-scrubbing-blacklist-alliances-comprehensive-guide), [The DNC Project](https://thedncproject.org/), [TCPA Litigator List](https://tcpalitigatorlist.com/).
- *Website/SEO signals:* BuiltWith, Wappalyzer.

Campaign presets were informed by:
[7 Essential Call Center Campaigns (Nextiva)](https://www.nextiva.com/blog/call-center-campaigns.html) ·
[B2B Telemarketing in 2026 (Cytranet)](https://cytranet.com/b2b-telemarketing-in-2026-how-to-reach-modern-buyers-and-drive-results/) ·
[Telemarketing Services 2026 Guide (Callzent)](https://callzent.com/telemarketing-services-2026-guide/)

- **Settings** — manage cities (pulls rotate through enabled cities,
  least-recently-pulled first), industries and their franchise/chain
  exclusion lists, the daily lead target, and two credit-costing toggles
  (contact enrichment, phone validation). ~40 industries ship by default
  (home trades, auto, and professional/health verticals); each has an
  editable Google Maps search phrase, and you can add your own custom
  industry with its own phrase (must contain `{city}`).

## Pulling, stopping, and verifying

- Pick one industry from the dashboard dropdown, set the count, and Pull; it
  works through cities and stops the instant the count is hit. A quick
  "＋ add industry" box on the dashboard creates a new industry and selects it
  for the next pull (full editing — search phrase, chain list — lives in Settings).
- A **Stop** button appears while a pull runs — it finishes the current query,
  keeps the leads gathered so far, and marks the run "cancelled".
- A **Clear** button dismisses the progress/result line and resets the day/status
  filters back to the default view.
- **Phone verification**: Outscraper's phones-enricher returns each number's
  line type (landline / mobile / voip) and flags dead numbers. Turn on
  "Validate phone numbers during each pull" in Settings to check every new
  lead automatically, or use the on-demand **Verify phones** button on the
  dashboard. Numbers that come back invalid are tagged and excluded from the
  VICIdial export. This costs one Outscraper credit per number.

### About owner / decision-maker names

Google Maps (Outscraper's source) does **not** carry business owner/CEO names,
so a named decision-maker is only available when the enrichment scrapes one
from the business website — uncommon for small local shops. When no name is
found the app falls back to inferring one from a personal email address
(`john.smith@… → John Smith`, tagged "from email") and skips role inboxes
(info@, sales@, …). Expect a real contact name on a minority of leads; the
email itself is the more reliable enrichment output.

## VICIdial export

The **VICIdial CSV** button produces a loader-ready file:

    Phone,Address3,Comments,Address1,City,State,PostCode,Website,Email,Show,

with `(812) 663-2886`-style phones, the search phrase in Address3, business
name (plus "ask for {owner}" when known) in Comments, 2-letter states, and
"No - Not on DNC" in the last column. Internally-flagged DNC numbers are never
included. Score / call hook / status columns stay in the app for agents — they
are not exported. Note: the DNC column reflects this app's own DNC list
(numbers imported from call logs); keep running your external federal-DNC
scrub if your campaign needs it.

Pulls also try to capture a decision-maker (owner / GM / CEO) and email via
Outscraper's Emails & Contacts enrichment (toggle in Settings — it costs extra
credits per lead), and run a small SEO probe of each new lead's website
(dead link, no HTTPS, missing title/description, not mobile-friendly) so the
agent has concrete talking points in the Call hook column.

## Legacy files (no longer used, safe to archive)

`daily_lead_puller.py`, `cities.txt`, `rotation_state.json`,
`seen_leads.sqlite3`, and the `leads_*.csv` files were the old CLI workflow.
Their data was imported into `leads.db` the first time the app started, so
phone numbers already dialed will never be served again. Use the Export CSV
button for dial sheets going forward.

## Compliance note

This pulls B2B contact data, which is generally treated differently from
consumer numbers under US telemarketing rules, but still run your own
DNC/complaint-list scrub before dialing.
