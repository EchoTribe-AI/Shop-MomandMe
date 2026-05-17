# Walmart "What's Trending Now" Workflow

> **Operational source of truth: [`docs/PG_LAUNCH_RUNBOOK.md`](PG_LAUNCH_RUNBOOK.md).**
> Read it first for production status, environment variables, admin auth model,
> and "never do this" lessons. This doc covers the Walmart trends workflow
> specifically; the runbook governs everything else.
>
> Companion doc: [`docs/amazon_trending_now.md`](amazon_trending_now.md) covers
> the Amazon Associates pipeline. The two share the admin UI, the refresh-run
> table, the collections tables, and the URLGenius cache.

## Setup assumptions

- **Production database**: Replit-managed PostgreSQL (auto-injected `DATABASE_URL`).
  Do **not** set `DATABASE_URL` manually — let the Replit PG service inject it.
- **Local dev fallback**: SQLite at `data/archer_catalog.db` when `DATABASE_URL`
  is unset. This file is `.gitignore`d and must never be committed.
- The bootstrap workbook is expected at `attached_assets/Walmart_May6th_Analysis.xlsx`
  by default. Other `Walmart_*.xlsx` files in `attached_assets/` can be selected
  via the admin UI dropdown.
- Existing Replit Secrets are reused:
  - `WALMART_API_PUBLIC_KEY`
  - `WALMART_API_PRIVATE_KEY`
  - `WALMART_PUBLISHER_ID`
  - `IMPACT_ACCOUNT_SID`
  - `IMPACT_AUTH_TOKEN`
  - `URLGENIUS_API_KEY`
- **Admin auth (preferred)**: session cookie from `/admin/login`. Default
  password is `dan` (override with `ADMIN_PASSWORD`). All Walmart trends admin
  endpoints accept a valid session.
- **Admin auth (legacy, external automation only)**: secret-token header for
  scripts/cron that can't hold a session. Configure one of:
  - `WALMART_TRENDS_ADMIN_TOKEN` (preferred name)
  - `ADMIN_API_TOKEN`
  - `ADMIN_SECRET`
  Send as `X-Walmart-Trends-Admin-Token: <token>` or `Authorization: Bearer <token>`.
  Do **not** embed this token into rendered HTML or browser JavaScript.
- Optional weekly Impact configuration:
  - `IMPACT_WALMART_CAMPAIGN_ID` defaults to `16662`.
  - `IMPACT_WALMART_PERFORMANCE_ENDPOINT` can override the account-specific
    product performance report endpoint. This endpoint must be validated against
    the configured Impact account/report.

## What is reused

- Walmart product lookup reuses `product_api.WalmartAPI`.
- Impact Walmart affiliate links reuse `product_api.ImpactAPI`.
- URLGenius link creation and registry dedupe reuse `product_api.URLGeniusAPI`.
- The page follows the existing Flask/Jinja template pattern and does not
  introduce a new frontend framework.

## Data model

The workflow creates normalized Walmart-specific tables via
`db_schema.init_schema()` (invoked by `db_schema.bootstrap()` and by the lazy
`app._ensure_schema_ready()` that fires on the first admin route hit):

- `walmart_products`
- `walmart_product_performance_snapshots`
- `walmart_affiliate_links`
- `walmart_urlgenius_links`
- `walmart_collections`
- `walmart_collection_items`
- `walmart_refresh_runs`

Each refresh run tracks source type, source window, source file, processed
counts, failures, status, and timestamps.

Schema setup is **fast, sync, idempotent, and never moves data**. New code
should use `db_schema._connect()` (not raw `sqlite3.connect`) so the same call
sites work against either PG or SQLite, and should prefer
`INSERT ... ON CONFLICT ... DO UPDATE` over SQLite-only `INSERT OR REPLACE`.

## Workbook bootstrap

The easiest path is the admin UI: visit `/walmart/trending-now?admin=1` (you'll
be redirected to `/admin/login` if not signed in), pick a `Walmart_*.xlsx` file
from the **Workbook Import** dropdown, and click Run.

You can also drive it from the shell:

```bash
python scripts/refresh_walmart_trends.py --mode bootstrap --workbook attached_assets/Walmart_May6th_Analysis.xlsx
```

or via HTTP with the legacy token header (use the session cookie instead when
possible):

```bash
curl -X POST http://localhost:5000/admin/walmart-trends/bootstrap \
  -H 'Content-Type: application/json' \
  -H 'X-Walmart-Trends-Admin-Token: <token>' \
  -d '{"workbook":"attached_assets/Walmart_May6th_Analysis.xlsx"}'
```

The bootstrap parser reads:

- `Trending - Item Count First` as source list `1A`
- `Trending - Earnings First` as source list `1B`
- `Curated Collections` as collection rows

The CLI output includes workbook diagnostics: workbook path, sheet names found,
parsed record counts, curated collection names, inserted/updated product and
collection counts, active collection count, first active collection slug, and
the first three SKUs in that collection.

It creates:

- One `Top Sellers` collection by combining `1A` and `1B`, deduping by SKU, and
  preserving `Top by Units` / `Top by Earnings` badges.
- One section for each workbook curated collection.

## Weekly refresh workflow

Run the recurring refresh with:

```bash
python scripts/refresh_walmart_trends.py --mode weekly
```

or:

```bash
curl -X POST http://localhost:5000/admin/walmart-trends/refresh \
  -H 'X-Walmart-Trends-Admin-Token: <token>'
```

The weekly process:

1. Pulls the latest 7-day Impact product performance window. The adapter logs
   the endpoint, date window, raw row count, rows with SKUs, skipped rows, and
   rows missing recognizable performance fields.
2. Aggregates rows by SKU.
3. Calculates item count, sale amount, and total earnings.
4. Builds top 10 by item count with earnings tiebreaker.
5. Builds top 10 by earnings with item count tiebreaker.
6. Builds category-led themed collections, targeting 8–10 products.
7. Enriches products from Walmart.
8. Generates or reuses Impact links.
9. Generates or reuses URLGenius links.
10. Replaces active page collections idempotently.

## Editing a published collection

- `/collections/<slug>/edit` loads in under a second; it uses
  `WalmartTrendStore.get_collection_by_slug(slug)`, which scopes the query to a
  single collection rather than loading all of them.
- Mobile-first editor: summary/status card → primary save action → quick
  actions → publishing card → content → collapsed secondary tools.
- Publish / unpublish / archive flows persist to the active database and
  survive refresh.

## Fallback behavior

- If Walmart product enrichment fails, workbook or Impact performance values
  remain available and the product is marked with fallback enrichment status.
- If Impact link generation fails, the workflow falls back to the best known
  Walmart product URL.
- If URLGenius is unavailable or `URLGENIUS_API_KEY` is missing, the Impact URL
  is used directly and recorded as a fallback URLGenius row.
- Refresh failures are stored in `walmart_refresh_runs.failures_json`; a
  partial run can still publish usable collections.
- Failed weekly Impact runs do not deactivate existing active collections, so
  the public page continues showing the last successful or partial published
  collection set.
- Refresh runs use a database-level guard (PG in production, SQLite in dev) to
  prevent overlapping bootstrap/weekly refreshes. A fresh `running` run blocks
  new refreshes; stale running rows older than two hours are marked failed
  before a new run starts.

## Landing page

- Public mobile page: `/walmart/trending-now`
- JSON data source: `/api/walmart/trending-now`

The page renders:

- Title: "What's Trending Now"
- Last refreshed timestamp
- Top Sellers section first
- One horizontal product rail per curated collection
- URLGenius CTA links when available, with Impact/Walmart fallback links
- Empty states when no active collections exist

The public render path uses `WalmartTrendStore.landing_page_data()` and does
**not** call `db_schema.bootstrap()` — schema setup is reserved for admin
routes via `_ensure_schema_ready()`.

## Cron / Replit scheduled deployment

Configure a weekly scheduled command similar to:

```bash
python scripts/refresh_walmart_trends.py --mode weekly
```

For first-time setup or reseeding from the workbook, run bootstrap manually
before enabling the weekly schedule. When deploying on Replit, the gunicorn
process runs with `--workers=1 --timeout=120` (see `.replit`); Cloud Run scales
via additional containers, not additional gunicorn workers.
