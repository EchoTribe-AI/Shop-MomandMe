# PostgreSQL Launch Runbook — `feature/pg-launch`

This is the deploy procedure for moving production from
`codex/published-page-persistence` (SQLite) to `feature/pg-launch`
(PostgreSQL with SQLite fallback for local dev).

**Goal**: zero data loss, single rollback path, mobile-tested.

**Slim-down (Step 7 of the original plan) is intentionally deferred** until
one week of soak time on PG. Do not delete routes or modules during this
deploy.

---

## Pre-flight checklist (verify before starting)

- [ ] `feature/pg-launch` HEAD on `origin` is the checkpoint commit (10
      commits ahead of `origin/main`)
- [ ] Tests green: `python3 -m unittest discover -s tests` → 256 ran,
      0 failures, 1 skipped
- [ ] A fresh `SECRET_KEY` generated and stored somewhere safe (a
      sample value was generated during planning; rotate it before deploy)
- [ ] PostgreSQL database provisioned and `DATABASE_URL` connection
      string in hand
- [ ] Replit shell access to the current `codex/published-page-persistence`
      deployment (needed for the SQLite snapshot)

---

## Phase A — Snapshot live SQLite (do this FIRST, before touching anything else)

The live data lives in `data/archer_catalog.db` on the running Replit
deployment. We need a frozen copy as the migration source.

**On the Replit shell of the current deployment:**

```bash
# 1. Confirm the live DB exists and check its size
ls -lh data/archer_catalog.db

# 2. Snapshot to a timestamped file in a writable, non-served location
SNAP="/tmp/archer_catalog_$(date +%Y%m%d_%H%M%S).db"
cp data/archer_catalog.db "$SNAP"
echo "Snapshot at: $SNAP"

# 3. Sanity-check row counts so you have a baseline to compare PG against
sqlite3 "$SNAP" <<'EOF'
SELECT 'collages', COUNT(*) FROM collages UNION ALL
SELECT 'posts', COUNT(*) FROM posts UNION ALL
SELECT 'walmart_products', COUNT(*) FROM walmart_products UNION ALL
SELECT 'amazon_trend_products', COUNT(*) FROM amazon_trend_products UNION ALL
SELECT 'walmart_affiliate_links', COUNT(*) FROM walmart_affiliate_links UNION ALL
SELECT 'walmart_urlgenius_links', COUNT(*) FROM walmart_urlgenius_links UNION ALL
SELECT 'walmart_collections', COUNT(*) FROM walmart_collections UNION ALL
SELECT 'walmart_collection_items', COUNT(*) FROM walmart_collection_items UNION ALL
SELECT 'collection_content_drafts', COUNT(*) FROM collection_content_drafts UNION ALL
SELECT 'creators', COUNT(*) FROM creators UNION ALL
SELECT 'click_log', COUNT(*) FROM click_log;
EOF
```

**Record the row counts.** You'll use them for the post-migration
verification in Phase D.

**Download the snapshot** to wherever you'll run the migration from
(local machine or a separate Replit shell that has both `psycopg2-binary`
and `DATABASE_URL` set). Use Replit's file browser or `scp` — whichever
is easiest.

---

## Phase B — Provision PostgreSQL

1. **Create the PostgreSQL database** in Replit (or Neon, Supabase, etc.).
   Note the full `DATABASE_URL` — must be in the form
   `postgresql://user:pass@host:port/dbname` (or `postgres://` — both work
   with psycopg2).

2. **Generate a strong `SECRET_KEY`** (Flask session signing). Don't reuse
   the example below — generate a fresh one:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. **Decide on `ADMIN_PASSWORD`.** Defaults to `dan`; override if you
   want a different admin password. Case-insensitive comparison.

---

## Phase C — Bootstrap schema + migrate data

The migration script handles bootstrapping the PG schema **and** copying
data. It uses `ON CONFLICT DO NOTHING` so re-running is safe (skips
existing rows, never overwrites).

**Where to run this**: any machine that has Python 3.10+, the repo
checkout, `psycopg2-binary` installed, both env vars set, and the SQLite
snapshot accessible. Easiest is the Replit shell of the *new* deployment
(or a temporary Replit shell), but it works locally too.

```bash
# Setup
cd /path/to/echo-dashboard           # repo on feature/pg-launch
git checkout feature/pg-launch
pip install psycopg2-binary           # if not already in your env

# Required env vars
export DATABASE_URL='postgresql://...your-pg-url...'
export CACHE_DB_PATH='/path/to/archer_catalog_<timestamp>.db'  # the snapshot from Phase A

# Run the migration. This bootstraps the schema first, then copies
# all 22 tables. Idempotent — safe to re-run.
python3 scripts/migrate_sqlite_to_postgres.py 2>&1 | tee /tmp/pg_migration.log
```

**What the log will show:**
- `Bootstrapping PostgreSQL schema …` → `Schema ready.` (creates all 22 tables idempotently)
- For each table: `OK    <table>: N rows inserted` or `EMPTY` or `SKIP`
- For each batch of 500 rows: `<table>: batch N committed`
- `Resetting PostgreSQL sequences …` — advances every `SERIAL.id` to `MAX(id)`
  so future inserts don't collide with migrated ids
- `Migration complete. Total rows inserted: NNNN`

**If any row fails**: the script logs `WARN  <table> row skipped: <reason>`
and continues. Re-run the script after fixing the underlying issue and
it'll pick up only the missing rows. Grep the log for `WARN` to see what
got skipped.

---

## Phase D — Verify row counts match

Run the same `COUNT(*)` queries from Phase A against PostgreSQL and
compare. Allow a small slop for `click_log` if traffic continued during
the snapshot window — everything else should match exactly.

```bash
psql "$DATABASE_URL" <<'EOF'
SELECT 'collages' AS t, COUNT(*) FROM collages UNION ALL
SELECT 'posts', COUNT(*) FROM posts UNION ALL
SELECT 'walmart_products', COUNT(*) FROM walmart_products UNION ALL
SELECT 'amazon_trend_products', COUNT(*) FROM amazon_trend_products UNION ALL
SELECT 'walmart_affiliate_links', COUNT(*) FROM walmart_affiliate_links UNION ALL
SELECT 'walmart_urlgenius_links', COUNT(*) FROM walmart_urlgenius_links UNION ALL
SELECT 'walmart_collections', COUNT(*) FROM walmart_collections UNION ALL
SELECT 'walmart_collection_items', COUNT(*) FROM walmart_collection_items UNION ALL
SELECT 'collection_content_drafts', COUNT(*) FROM collection_content_drafts UNION ALL
SELECT 'creators', COUNT(*) FROM creators UNION ALL
SELECT 'click_log', COUNT(*) FROM click_log;
EOF
```

**Acceptable variances:**
- `walmart_urlgenius_links` / `walmart_affiliate_links` may show slightly
  fewer rows if duplicates collided on the UNIQUE constraint
  (`ON CONFLICT DO NOTHING` — first row wins; acceptable)
- `click_log` may grow during the snapshot/migration window

**Not acceptable** (investigate before proceeding):
- `collages`, `posts`, `creators`, `walmart_collections`,
  `walmart_collection_items`, `collection_content_drafts` — any
  mismatch here means a real row was dropped. Re-run the migration with
  the same snapshot; verify the warn log.

If `creators` is empty after migration, run `db_schema.bootstrap()` on
the PG database — it re-seeds the default `everydaywithsteph` creator.

---

## Phase E — Deploy `feature/pg-launch`

1. **In Replit, set deployment env vars** (Secrets tab):
   - `DATABASE_URL` — the PG connection string from Phase B
   - `SECRET_KEY` — the value generated in Phase B
   - `ADMIN_PASSWORD` — optional, defaults to `dan`
   - **Keep all existing secrets** (`ANTHROPIC_API_KEY`, `WALMART_API_KEY`,
     `IMPACT_ACCOUNT_SID`, `IMPACT_AUTH_TOKEN`, `URLGENIUS_API_KEY`,
     `WALMART_TRENDS_ADMIN_TOKEN`, etc.) — `feature/pg-launch` still uses
     them.

2. **Switch the deployment branch** from
   `codex/published-page-persistence` to `feature/pg-launch`. (Replit
   deploy → Branch.)

3. **Deploy.** Watch the boot logs for:
   - No `db_schema.bootstrap failed` warnings
   - No `psycopg2.OperationalError` (means `DATABASE_URL` is wrong)
   - First request lands without `500`

4. **Confirm SQLite fallback didn't accidentally engage:** boot logs
   should show no references to `data/archer_catalog.db` after deploy
   (everything should flow through PG).

---

## Phase F — Smoke test (mobile-first; user uses 99% mobile)

On a real phone, in order:

1. **Auth flow**
   - Open `/hub` → redirect to `/admin/login`
   - Enter password (`dan` or your `ADMIN_PASSWORD`) → bounce to `/hub`
   - Refresh `/hub` → still authorized

2. **Trending Now (admin)**
   - `/walmart/trending-now?admin=1` → trends + collections render
   - Workbook import button works (this is where the BEGIN IMMEDIATE
     and `datetime('now', ...)` fixes were applied — must not 500)

3. **Editor flow**
   - `/collections/<some-slug>/edit` → mobile layout per `a052459`
     redesign: summary card, single primary save action, quick actions,
     publishing card, content below, collapsed secondary tools
   - Theme picker shows `mommyme` first and selected by default for new
     drafts; existing drafts keep their saved theme
   - Edit → save → reload → changes persist
   - Edit published → republish → live page reflects current editor state
     (PG publish CASE fix)
   - Publish → set to draft → public 404, preview still works
   - Publish → archive → public 404, manage shows it, survives refresh
   - Archive → restore → manage status correct

4. **Posts**
   - `/archer/posts/manage` → list of posts
   - `/archer/posts/<id>/edit` → smart-link creation works without
     manually filling utm_campaign (defaultCampaign() fallback)
   - The `utm_campaign` input is pre-filled with the derived default;
     editing collection_slug or angle updates it (as long as the user
     hasn't manually edited)

5. **Public shop**
   - `/shop/<slug>` → published collection renders
   - `/api/shop/chat` → POST `{"message": "show me toys"}` returns
     products with affiliate links

6. **Confirm zero `?admin_token=` URLs** anywhere in the admin UI
   (view-source on `/hub`, `/walmart/trending-now?admin=1`,
   `/archer/posts/manage`).

---

## Phase G — Rollback (if any smoke test fails)

The SQLite database at `data/archer_catalog.db` on the
`codex/published-page-persistence` deployment is **untouched** —
no migration script writes back to SQLite. To roll back:

1. In Replit, switch the deployment branch back to
   `codex/published-page-persistence`.
2. Deploy.
3. App boots on SQLite again with all data intact.

PG data persists in the provisioned database in case you want to retry
later. You can drop and recreate the PG database, then re-run the
migration with a fresh snapshot.

---

## Phase H — Soak (one week before slim-down)

Per the launch plan: **do not start the Step 7 slim-down for one week**.
Use this time to:
- Verify trend refresh runs cleanly in PG
- Catch any rare-path SQL incompatibility that didn't show up in the
  256 unit tests
- Confirm the admin session model holds up across deploys / cookie
  rotations

After a week of clean operation, branch from `feature/pg-launch` to
start the slim-down (delete dropped routes, modules, templates).
See `harmonic-purring-hippo.md` plan, Steps 7–8.

---

## Reference: what's in this checkpoint

`feature/pg-launch` (10 commits ahead of `origin/main`):

| Commit | Title |
|---|---|
| `3d6b086` | fix: 4 PG-launch regressions (publish CASE, BEGIN IMMEDIATE, default theme, smart-link campaign) |
| `3499190` | feat: add 'mommyme' theme to both registries |
| `bd7011d` | chore: drop admin_token from template URLs and fetch headers |
| `ee408ec` | feat: server-side admin session auth; tighten `_walmart_content_demo_allowed` |
| `77b551d` | fix: walmart_trends.create_run cursor order; update 2 tests post-merge |
| `1d13372` | Redesign mobile collection publishing workflow (cherry-pick) |
| `950c935` | Simplify collection editor actions (cherry-pick) |
| `c3c4375` | Fix hub admin navigation links (cherry-pick) |
| `88c009d` | Polish admin header and publishing status flow (cherry-pick) |
| `2634679` | Fix published page persistence resolution (cherry-pick) |

**On `origin/main`** (Replit Agent's tested PG migration — the base):
- `db_schema.py` `_PGConn` adapter wraps psycopg2 to mimic sqlite3 API
- `_adapt_sql` translates `?` → `%s`, `datetime('now', ...)` → PG INTERVAL,
  `BEGIN IMMEDIATE` → no-op
- `scripts/migrate_sqlite_to_postgres.py` — what you ran in Phase C
- `scripts/prod_seed.sql` — 5,081-line snapshot from 2026-05-15
  (backup recovery only — `Phase A` snapshot is fresher)
- `/admin/seed-production` POST endpoint — last-resort recovery; runs
  `prod_seed.sql`. Use only if Phase A snapshot is unavailable.
