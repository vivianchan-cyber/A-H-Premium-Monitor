# Deploying the A–H Premium Monitor to Railway (private team website)

This guide takes you from a private GitHub repository to a
password-protected website your colleagues can use, with a hosted
PostgreSQL database and automatic data refresh. No prior Railway
experience is assumed. Everything you type is shown in `code blocks`.

**What you end up with**

- `https://<your-app>.up.railway.app` (and optionally your own domain)
- login limited to the e-mail addresses you approve (admin / viewer roles)
- PostgreSQL holding all market data (prices, history, HSAHP, alerts…)
- a cron service refreshing data during HK/mainland market hours

---

## 0. Prerequisites (one-time)

1. A GitHub account with this repository pushed as a **Private** repo
   (GitHub → repo → Settings → General → Danger Zone shows visibility).
2. A Railway account: <https://railway.app> → "Login with GitHub".
3. Python installed locally (you already have it if you ran the app).

---

## 1. Create the Railway project and the PostgreSQL database

1. Railway dashboard → **New Project** → **Deploy PostgreSQL**.
   This creates a project containing one service called `Postgres`.
2. Click the `Postgres` service → **Variables** tab. Railway manages the
   credentials for you; you never need to copy the password anywhere —
   other services will *reference* it (step 3).

## 2. Deploy the app from your private GitHub repository

1. In the same project: **+ New** → **GitHub Repo** → authorize Railway
   to see your private repositories if asked → pick
   `A-H-Premium-Monitor` and the branch to deploy.
2. Railway builds automatically (it detects Python from
   `requirements.txt`; the start command comes from `railway.toml`).
   The **first deploy will crash** — expected, because the environment
   variables aren't set yet. Continue.

## 3. Configure DATABASE_URL

1. Click your app service → **Variables** → **New Variable** →
   **Add Reference** → choose `Postgres.DATABASE_URL`.
   This injects the connection string without ever exposing it in code.

## 4. Configure the approved users (logins)

1. On your own computer, generate one password hash per person:

   ```bash
   python -m ahmon.auth hash
   ```

   Type the person's password twice; copy the printed
   `pbkdf2_sha256$…` line. Repeat per user.
2. In the app service → **Variables** → **New Variable**:
   - Name: `AHMON_USERS`
   - Value (one line; your real e-mails and the hashes from step 1):

   ```json
   [{"email": "you@company.com", "role": "admin", "password_hash": "pbkdf2_sha256$..."},
    {"email": "colleague@company.com", "role": "viewer", "password_hash": "pbkdf2_sha256$..."}]
   ```

   Roles: `admin` can refresh data, run backfills, manage the Focus
   list and use system actions; `viewer` is read-only. Only e-mails in
   this list can log in at all.
3. **Redeploy** (Deployments → ⋮ → Redeploy). The app now starts and
   shows the login page.

## 5. Load your data into PostgreSQL

From your computer, copy your local database into the hosted one:

```bash
# 1) get the database URL for external use (only for this command):
#    Railway → Postgres service → Variables → DATABASE_PUBLIC_URL → copy
# 2) run the migration against your local live file:
DATABASE_URL="<paste DATABASE_PUBLIC_URL>" \
  python -m ahmon.migrate --sqlite data/ahmon_live.db --replace
```

Expected output: a per-table row count ending in something like
`Migrated 227,000 rows across 11 tables`. If you have no local data yet,
skip this — an admin can press "Refresh live data now" and use the
Health-tab backfill button instead (or run
`DATABASE_URL=… python -m ahmon.backfill` locally overnight).

> The `DATABASE_PUBLIC_URL` contains a password. Paste it only into your
> terminal — never into a file that gets committed.

## 6. Create the scheduled refresh (cron service)

1. Project → **+ New** → **GitHub Repo** → choose the **same repository
   again** (this becomes a second service; rename it to `cron-refresh`
   via its Settings → Service Name).
2. `cron-refresh` service → **Settings**:
   - **Start Command**: `python -m ahmon.cron_refresh`
   - **Cron Schedule**: `*/5 * * * *`
     (Railway crons run in **UTC**; the command itself checks HK/SSE
     market hours and exits instantly when closed, so every-5-minutes
     is safe and nearly free outside trading hours.)
3. `cron-refresh` → **Variables** → Add Reference →
   `Postgres.DATABASE_URL` (same as step 3; no AHMON_USERS needed).
4. Optional but recommended, a second cron service for the post-close
   snapshot + commentary:
   - Start Command: `python -m ahmon.cron_refresh --eod --force`
   - Cron Schedule: `20 8 * * 1-5`  (08:20 UTC = 16:20 HK time)
5. Only-one-writer guarantee: every cron run takes a PostgreSQL
   advisory lock before writing; an overlapping run logs
   "another writer holds the refresh lock — skipping" and exits cleanly.

## 7. Generate the Railway domain

App service → **Settings** → **Networking** → **Generate Domain**.
You get `https://<something>.up.railway.app`. The health check
(`/_stcore/health`, configured in `railway.toml`) must show green in
the Deployments tab.

## 8. Connect a custom domain (optional)

1. App service → Settings → Networking → **Custom Domain** → enter e.g.
   `ahmon.yourdomain.com`.
2. Railway shows a **CNAME target**. In your DNS provider (Cloudflare,
   Namecheap, …) create: `CNAME  ahmon  <target railway shows>`.
3. Wait for DNS + certificate (minutes to an hour). Railway shows a
   green check when live; HTTPS is automatic.

## 9. Test authentication

1. Open the URL in a private/incognito window → you must see **only**
   the login page (no data).
2. Wrong password → "Unknown email or wrong password."
3. An e-mail not in `AHMON_USERS` → same rejection (approved list is
   absolute).
4. Log in as a **viewer** → confirm there is *no* "Refresh live data
   now" button, no CSV import, no reclassify controls, no backfill
   button.
5. Log in as an **admin** → confirm those controls exist and the
   Health tab shows all sources.
6. Log out via the sidebar button → back to the login page.

## 10. Rolling back a failed deployment

1. App service → **Deployments**: every previous build is listed.
2. Find the last working deployment → **⋮ → Redeploy**. That exact
   build goes live again (the database is untouched — rollback only
   affects code).
3. If a bad deploy also wrote bad *data*, restore the database:
   Postgres service → **Backups** → restore the latest snapshot
   (enable daily backups there the first time you visit).
4. Keep the local SQLite file from before the migration — it is a full
   offline copy of your history and can be re-migrated any time with
   `python -m ahmon.migrate --replace`.

---

## Reference: services, commands, variables

| Service | Start command | Cron | Variables |
|---|---|---|---|
| web (dashboard) | `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true` (from `railway.toml`) | — | `DATABASE_URL` (reference), `AHMON_USERS` |
| cron-refresh | `python -m ahmon.cron_refresh` | `*/5 * * * *` | `DATABASE_URL` (reference) |
| cron-eod (optional) | `python -m ahmon.cron_refresh --eod --force` | `20 8 * * 1-5` (UTC) | `DATABASE_URL` (reference) |
| Postgres | managed by Railway | — | managed |

Build command: none needed — Nixpacks runs
`pip install -r requirements.txt` automatically.

## Security notes

- Secrets live **only** in Railway variables: `DATABASE_URL` (injected
  by reference) and `AHMON_USERS` (hashes, never plaintext passwords).
  Nothing sensitive is in the repository, the logs, or the page source.
- The repo must stay **private**: `config/portfolio_sample.csv` seeds
  the Focus list. Day-to-day portfolio management should happen in the
  app (stored in PostgreSQL), not by committing CSV changes.
- Streamlit is configured with XSRF protection on and error details
  hidden from the browser (`.streamlit/config.toml`).
- Local development is unchanged: without `DATABASE_URL` the app uses
  the local SQLite files and, if `AHMON_USERS` is also unset, runs
  open with a visible "local development mode" notice.
