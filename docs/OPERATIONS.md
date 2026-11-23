# Operations

How to back up and restore the production database, what the app reads from
the environment, and how to roll a deploy back. Production is one Railway
project with two services: the web app (gunicorn, from this repo) and a
Postgres database linked to it through `DATABASE_URL`.

This file deliberately contains no keys, hostnames or client names. Real values
live only in Railway → service → **Variables**.

## 1. Database backups (Postgres on Railway)

### Take a backup

**Railway's own backups.** Open the Postgres service in the Railway project and
use its **Backups** tab. Railway keeps scheduled snapshots there and lets you
create one on demand. Take a manual one before any deploy that changes the
schema (anything touching `db.py`), and before a bulk import or delete.

**A file you hold yourself** with `pg_dump`, using the `DATABASE_URL` variable
of the Postgres service. From a machine with the Postgres client tools installed
and the Railway CLI linked to the project:

```
railway variables --service <postgres-service> --kv | grep ^DATABASE_URL=
pg_dump "$DATABASE_URL" --format=custom --no-owner --no-privileges \
        --file "leads-$(date +%Y-%m-%d).dump"
```

or, if you prefer not to copy the URL into a shell variable:

```
railway run --service <postgres-service> \
    pg_dump "$DATABASE_URL" --format=custom --no-owner --no-privileges \
    --file "leads-$(date +%Y-%m-%d).dump"
```

Use the **public** connection URL (`DATABASE_PUBLIC_URL`) when running from
outside Railway; the internal URL only resolves inside the project's network.
Keep the dump somewhere private: it contains every lead, phone number and
email the app holds.

### Restore a backup

**From Railway's Backups tab:** pick the snapshot and restore it. Railway
restores into the same database; the app needs no change, only a restart of
the web service afterwards so every worker reconnects.

**From a `pg_dump` file:**

```
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname "$DATABASE_URL" "leads-YYYY-MM-DD.dump"
```

`--clean --if-exists` drops and recreates the tables the dump contains, so the
database ends up exactly as it was at backup time. Stop or pause the web
service first (or expect a few failed requests while tables are rebuilt), then
restart it. On start the app runs its own idempotent migrations
(`db.init_db`), so a dump from an older release is upgraded automatically.

### Checking a backup is usable

Restore it into a throwaway database (a second Postgres service, or a local
one) and run the app against it with `DATABASE_URL` pointing there. The
Settings page shows the storage the app is using; History shows the lead count.

## 2. Environment variables

Set in Railway → web service → **Variables**. Locally the same names are read
from the environment or from a `.env` file next to `app.py` (never commit it).
The app starts with any optional variable missing; the feature that needs it
simply stays off, usually with a note in Settings saying what to set.

### Required in production

| Variable | What it does | If missing |
|---|---|---|
| `SECRET_KEY` | Signs the login session cookie. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. | **The app refuses to start** on Railway (or when `REQUIRE_SECRET_KEY=1`) and logs why. Locally a random key is used per start, so sessions end on restart. |
| `APP_PASSWORD` (or `ADMIN_PASSWORD`) | Turns login on and seeds the first admin account (`ADMIN_USER`, default `admin`). | **The site is open to anyone with the URL**, including the credit-spending pull button. Locally this is the normal single-user mode. |
| `DATABASE_URL` | Postgres connection string. Railway injects it when the Postgres service is linked. | The app uses a SQLite file in `DATA_DIR` instead — fine locally, wrong in production. |
| `OUTSCRAPER_API_KEY` | Google Maps data and phone validation (the core lead source). | Pulls and phone verification return an error; everything else works. |

### Optional – data and storage

| Variable | What it does | If missing |
|---|---|---|
| `DATA_DIR` | Folder for the SQLite file, the boot counter and `seed_clients.json` (default: the code folder). | Defaults apply. Irrelevant on Postgres except for the seed file. |
| `DB_CONNECT_TIMEOUT` | Seconds to wait for Postgres on start (default 15). | Default. |
| `VOLUME_WAIT_SECONDS` | How long a SQLite deploy waits for its volume to mount (default 25). | Default. |
| `SEED_CLIENTS_FILE` | Path to a JSON list of client engagements to create once on first start (see `seed_clients.example.json`). | Nothing is seeded. Existing databases are never re-seeded either way. |
| `ENABLE_SCHEDULER` | `0` disables the in-process nightly requeue scheduler (tests do this). | Scheduler runs (default `1`). |

### Optional – accounts

| Variable | What it does | If missing |
|---|---|---|
| `ADMIN_USER` | Username of the seeded admin (default `admin`). | `admin`. |
| `ADMIN_RESET` | `1` forces the admin account to `ADMIN_PASSWORD` on the next start — the way back in when the password is lost. Remove it afterwards. | Normal behaviour. |
| `REQUIRE_SECRET_KEY` | `1` applies the production `SECRET_KEY` rule outside Railway. | Production is detected from Railway's own variables. |

### Optional – paid providers (each dormant without its key)

| Variable | What it does | If missing |
|---|---|---|
| `APOLLO_API_KEY` | Apollo contact enrichment (decision-maker name/title, email and direct-dial reveals, company size). | Enrichment runs only the free website tier; Apollo buttons say so. |
| `ANTHROPIC_API_KEY` | Claude: AI industry suggestions, and reading a team page for a decision-maker when the free pattern matcher finds nobody. `ANTHROPIC_MODEL` overrides the model named in `claude_config.py`; `SITE_CONTACTS_AI_MAX_CALLS` caps page reads per enrichment run (default 40). | Both features are off. |
| `EMAIL_VERIFY_API_KEY` | Email deliverability checks; `EMAIL_VERIFY_PROVIDER` picks the provider (default `zerobounce`). | The Verify emails button is disabled. |
| `DNC_API_KEY`, `DNC_PROVIDER` | External DNC / litigator scrub provider. | Only the uploaded suppression lists apply. |
| `INTAKE_API_KEY` | Enables the public lead-intake webhook (`X-Intake-Key` header). | The endpoint answers 503; CSV import still works. |
| `SIGNAL_API_KEY` | Enables the intent-signal webhook. | The endpoint answers 503. |
| `OPS_DB_HOST`, `OPS_DB_PORT`, `OPS_DB_NAME`, `OPS_DB_USER`, `OPS_DB_PASSWORD` (or the same names without `OPS_`) | Read-only connection to the dialer's MySQL for dispositions and callbacks. | Disposition import, requeue and callback views are off. |

Every paid provider is also subject to a **daily cap** (Settings → Daily caps),
counted per provider per day in the database. Defaults are deliberately low;
`0` means no limit and must be chosen on purpose.

Railway itself sets `RAILWAY_*` variables (environment name, deployment id,
volume paths) and `PORT`; the app reads them for the Settings storage panel and
to detect that it is running in production. Do not set them by hand.

## 3. Rolling back a deploy

Every push to the connected branch creates a deployment. To go back:

1. Open the web service in Railway and go to **Deployments**.
2. Find the last deployment that was good (each one shows its commit message
   and time).
3. Open its menu (⋮) and choose **Redeploy**. Railway rebuilds that commit
   and switches traffic to it when it is healthy; the current deployment is
   stopped.

Two things a rollback does **not** undo:

- **Database changes.** Migrations add columns and tables and never drop or
  rewrite lead rows, so an older release runs fine against a newer schema
  (it ignores columns it does not know). Data written by the newer release
  stays. If a release corrupted data, restore the backup taken before it
  (section 1) after rolling the code back.
- **Variables.** A redeploy uses the variables as they are now. If the
  rollback is because a variable was changed, change it back first.

Check after rolling back: the login page loads, Settings shows the expected
storage and today's provider usage, and the Dashboard shows the lead count you
expect.
