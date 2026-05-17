# Amazon Associates Trends Workflow

> **Operational source of truth: [`docs/PG_LAUNCH_RUNBOOK.md`](PG_LAUNCH_RUNBOOK.md).**
> Read it first for production status, environment variables, admin auth model,
> and "never do this" lessons. This doc covers the Amazon trends workflow
> specifically; the runbook governs everything else.
>
> Companion doc: [`docs/walmart_trending_now.md`](walmart_trending_now.md).
> The two pipelines share the workbook admin UI, the refresh-run table, the
> collections tables, and the URLGenius cache.

## Setup assumptions

- **Production database**: Replit-managed PostgreSQL (auto-injected
  `DATABASE_URL`). Do **not** set `DATABASE_URL` manually — let the Replit PG
  service inject it.
- **Local dev fallback**: SQLite at `data/archer_catalog.db` when
  `DATABASE_URL` is unset. This file is `.gitignore`d and must never be
  committed.
- Bootstrap workbooks are expected in `attached_assets/` and named
  `Amazon_*.xlsx`. Routing is filename-prefix driven — `Walmart_*.xlsx` files
  go to the Walmart service, `Amazon_*.xlsx` to the Amazon service, via the
  same `/admin/walmart-trends/bootstrap` endpoint.
- Replit Secrets:
  - **Amazon Creators API (primary enrichment)** — at least one naming
    convention from each pair must be set:
    - `AMAZON_CREATORS_CLIENT_ID` *or* `CREDENTIAL_ID`
    - `AMAZON_CREATORS_CLIENT_SECRET` *or* `CREDENTIAL_SECRET`
    - `AMAZON_CREATORS_CREDENTIAL_VERSION` (no default; required to pick the
      correct token endpoint family)
    - `AMAZON_PARTNER_TAG` *or* `AMAZON_AFFILIATE_TAG`
    - `AMAZON_MARKETPLACE` (optional; defaults to the module's
      `DEFAULT_MARKETPLACE`)
  - **Crawlbase (fallback enrichment)**:
    - `CRAWLBASE_JS_TOKEN`
  - **Affiliate tagging**:
    - `AMAZON_AFFILIATE_TAG` (default `mommymedeals-20`) — appended to product
      URLs only when no `tag` param is already present.
  - **URLGenius (shared with Walmart)**:
    - `URLGENIUS_API_KEY`
- **Admin auth (preferred)**: session cookie from `/admin/login`. Default
  password is `dan` (override with `ADMIN_PASSWORD`). All Amazon trends admin
  endpoints accept a valid session.
- **Admin auth (legacy, external automation only)**: secret-token header for
  scripts/cron that can't hold a session. Configure one of:
  - `WALMART_TRENDS_ADMIN_TOKEN` (preferred name)
  - `ADMIN_API_TOKEN`
  - `ADMIN_SECRET`
  Send as `X-Walmart-Trends-Admin-Token: <token>` or
  `Authorization: Bearer <token>`. Do **not** embed this token into rendered
  HTML or browser JavaScript.

## What is reused

- Workbook XLSX parsing reuses `walmart_trends.WorkbookTrendParser` (Amazon
  parser inherits and overrides sheet mappings).
- Run lifecycle (`walmart_refresh_runs`), collection persistence
  (`walmart_collections`, `walmart_collection_items`), and the URLGenius cache
  (`walmart_urlgenius_links`) are shared with the Walmart pipeline, scoped
  via `retailer='amazon'`.
- URLGenius wrapping reuses `product_api.URLGeniusAPI`.
- Crawlbase fallback enrichment reuses `product_api.CrawlbaseAPI`.
- The admin UI and JSON API live alongside the Walmart routes; no separate
  frontend framework is introduced.

## Data model

Amazon-specific tables (created by `db_schema.init_schema()`, invoked via
`db_schema.bootstrap()` and the lazy `app._ensure_schema_ready()`):

- `amazon_trend_products`
- `amazon_product_performance_snapshots`
- `amazon_affiliate_links`

Shared tables (Amazon rows scoped by `retailer='amazon'`):

- `walmart_refresh_runs` — source type `amazon_workbook_bootstrap`
- `walmart_collections`, `walmart_collection_items`
- `walmart_urlgenius_links` — retailer-agnostic destination URL cache

Each refresh run tracks source type, source file, processed counts, failures,
status, and timestamps.

New code should use `db_schema._connect()` (not raw `sqlite3.connect`) and
prefer `INSERT ... ON CONFLICT ... DO UPDATE` over SQLite-only
`INSERT OR REPLACE` so the same call sites work against either PG or SQLite.

## Workbook bootstrap

The easiest path is the admin UI: visit `/walmart/trending-now?admin=1`
(you'll be redirected to `/admin/login` if not signed in), pick an
`Amazon_*.xlsx` file from the **Workbook Import** dropdown, and click Run.
Amazon imports **auto-trigger** a prioritized enrichment pass (limit 30,
max_workers 4) immediately after the import completes.

You can also call the endpoint directly. The same `/admin/walmart-trends/bootstrap`
route handles Amazon files — routing is determined by the filename prefix:

```bash
curl -X POST http://localhost:5000/admin/walmart-trends/bootstrap \
  -H 'Content-Type: application/json' \
  -H 'X-Walmart-Trends-Admin-Token: <token>' \
  -d '{"workbook":"attached_assets/Amazon_Trends_May12.xlsx"}'
```

The Amazon workbook parser reads:

- `Trending - Items Ordered` as source list `2A`
- `Trending - Items Shipped` as source list `2B`
- `Trending - Earnings` as source list `2C`
- `Curated Collections` as collection rows

(See `AmazonWorkbookParser` in `amazon_trends.py` for the canonical
sheet-name mapping.)

Diagnostics logged on bootstrap include: workbook path, sheet names found,
parsed record counts per source list, curated collection names, inserted /
updated product and collection counts, and counts of seeded workbook
affiliate links and URLGenius links.

Bootstrap is **decoupled from enrichment** — new rows default to
`enrichment_status='pending'`. Image, price, and brand are backfilled in a
separate pass (see next section). This keeps imports fast and avoids blocking
on Crawlbase scraping during ingestion.

## Enrichment (backfill image / price / brand)

`AmazonProductEnricher` runs in two phases per batch:

1. **Amazon Creators API** (primary) — `GetItems` in batches of up to 10
   ASINs. The vended `detailPageURL` is stored verbatim in `detail_page_url`;
   altering returned URL parameters can break affiliate attribution.
2. **Crawlbase JS-rendered PDP scrape** (fallback) — used only when the
   Creators API is unconfigured, returns no row for an ASIN, or returns a row
   missing both image AND price. Requires `CRAWLBASE_JS_TOKEN`.

Trigger a prioritized enrichment pass from the admin UI's
**"Enrich Amazon prices/images"** button or via API:

```bash
curl -X POST http://localhost:5000/admin/amazon-trends/enrich \
  -H 'Content-Type: application/json' \
  -H 'X-Walmart-Trends-Admin-Token: <token>' \
  -d '{"limit": 30, "max_workers": 4}'
```

`limit` is clamped to `[1, 200]`; `max_workers` to `[1, 16]`. Default values
are `30` and `4`, matching the auto-enrichment that runs after a workbook
import.

The response counts include:

- `ok` — enriched successfully
- `pending` — still missing critical fields after this pass
- `fallback` — completed via Crawlbase (not Creators API)
- `skipped` — already enriched, no work needed
- `creators` / `crawlbase` — which backend served each ASIN
- `queued` — total ASINs selected for this run
- `missing_rows` — ASINs the API was asked about that have **no row in
  `amazon_trend_products`** at all. If non-zero, those ASINs need to be
  (re-)imported via a workbook, not just re-enriched. (Counter introduced to
  surface the "API said success but the row didn't exist" class of bug.)

Prioritization for `enrich_pending`: ASINs in active collections first, then
rows missing image OR price, ordered by recency. Rows already marked
`enrichment_status='ok'` with an image are skipped.

Fatal Creators-API misconfigurations (e.g. bad partner tag, ineligible
associate, validation errors) abort the run rather than silently falling
back to Crawlbase for every ASIN — fixing the config is the only correct
action.

## Fallback behavior

- If Creators API enrichment is unconfigured or returns no critical fields,
  Crawlbase is used and the row is marked `enrichment_status='fallback'`.
- If both backends fail, the row stays `pending` with an error stored;
  workbook-derived values (title, link, performance metrics) remain usable.
- If URLGenius is unavailable or `URLGENIUS_API_KEY` is missing, the
  tagged Amazon URL is used directly and recorded as a fallback URLGenius row.
- Bootstrap failures are stored in `walmart_refresh_runs.failures_json`; a
  partial run can still publish usable collections.
- Refresh runs use a database-level guard (PG in production, SQLite in dev)
  via `walmart_refresh_runs` to prevent overlapping bootstraps. Stale
  `running` rows older than two hours are marked failed before a new run
  starts.

## Landing page

Amazon collections are surfaced through the same shared landing pages as
Walmart collections; the storefront renders by collection slug, not by
retailer. Each Amazon collection's items use the `retailer='amazon'` scope
on `walmart_collection_items`.

- Editor (per collection): `/collections/<slug>/edit` — narrow query via
  `WalmartTrendStore.get_collection_by_slug()`.
- Publish / unpublish / archive flows persist to the active database and
  survive refresh.

## No Impact-style weekly refresh

Unlike the Walmart pipeline, **the Amazon workflow has no automated
performance-API refresh**. There is no `refresh_from_impact()` equivalent on
`AmazonTrendRefreshService` — only `bootstrap_from_workbook()` and
`enrich_pending()`. Refresh cadence is operator-driven:

1. Export the latest Amazon Associates trend report to `attached_assets/` as
   `Amazon_*.xlsx`.
2. Run a workbook bootstrap (UI or API).
3. The post-import enrichment auto-fires (limit 30, max_workers 4).
4. If `missing_rows` was non-zero on a separate enrichment call, re-import
   the workbook — those ASINs don't exist in `amazon_trend_products` yet.
5. For larger backfills, post directly to the enrichment endpoint with a
   higher limit, e.g. `{"limit": 200, "max_workers": 4}`.

## Tests

```bash
python3 -m unittest tests.test_amazon_phase3
```

Covers Creators + Crawlbase enrichment paths, `product_title` write on
enrichment, and `update_product_enrichment` rowcount safety.
