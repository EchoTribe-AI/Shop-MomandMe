# Fresh PostgreSQL Launch Runbook

This branch launches EchoTribe on PostgreSQL with a clean database. It does
not copy the historical SQLite catalog at startup. New workbook imports are the
source of truth going forward.

## What This Branch Does

- Creates/patches the PostgreSQL schema at app boot.
- Seeds only the default creator row.
- Keeps `/healthz` for fast deployment health checks.
- Keeps the one-worker, longer-timeout Autoscale deployment command.
- Leaves `scripts/migrate_sqlite_to_postgres.py` available as an explicit
  manual tool only.

## What This Branch Does Not Do

- It does not commit `data/archer_catalog.db`.
- It does not auto-seed PostgreSQL from SQLite.
- It does not restore old published pages, drafts, posts, clicks, cached
  affiliate links, or imported trend products.
- It does not run the SQLite migration during app startup.

## Deploy

1. Deploy `codex/fresh-pg-launch`.
2. Set Replit secrets:
   - `DATABASE_URL`
   - `SECRET_KEY` or `FLASK_SECRET_KEY`
   - `ADMIN_PASSWORD` if you do not want the default `dan`
   - all existing API/affiliate secrets already used by the app
3. Do not run the SQLite migration for a fresh launch.
4. Open `/admin/login` and sign in.
5. Open `/walmart/trending-now?admin=1`.
6. Import the new Walmart/Amazon reports.
7. Build and publish fresh collections from those imports.

## Smoke Test

- `/healthz` returns `ok`.
- `/admin/login` accepts the admin password.
- `/hub` loads after login.
- `/walmart/trending-now?admin=1` renders even before imports.
- Workbook import populates trends/collections.
- `/archer/posts/manage` loads.
- A new collection can be edited, previewed, published, drafted, and archived.
- A published `/shop/<slug>` page renders publicly.

## Optional Manual Migration

If historical data is ever needed later, use the migration script explicitly:

```bash
export DATABASE_URL='postgresql://...'
export CACHE_DB_PATH='/path/to/sqlite-snapshot.db'
python3 scripts/migrate_sqlite_to_postgres.py
```

The script creates schema/default creator directly with `init_schema()` and
`seed_default_creator()`. It does not rely on app startup `bootstrap()`.

## Rollback

If the fresh PG launch has issues, switch deployment back to the previous
branch. This branch does not delete the old Replit SQLite file; it simply does
not package or auto-seed from it.
