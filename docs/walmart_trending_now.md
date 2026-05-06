# Walmart “What’s Trending Now” Workflow

## Setup assumptions

- The app uses the existing SQLite cache database at `data/archer_catalog.db`.
- The bootstrap workbook is expected at `attached_assets/Walmart_May6th_Analysis.xlsx` by default.
- The prompt also referenced `attached_assets/walmart_14_day_product_analysis_may6.xlsx`, but that file was not present in this checkout.
- Existing Replit Secrets are reused:
  - `WALMART_API_PUBLIC_KEY`
  - `WALMART_API_PRIVATE_KEY`
  - `WALMART_PUBLISHER_ID`
  - `IMPACT_ACCOUNT_SID`
  - `IMPACT_AUTH_TOKEN`
  - `URLGENIUS_API_KEY`
- Admin refresh endpoints require a secret token. Configure one of:
  - `WALMART_TRENDS_ADMIN_TOKEN` (preferred)
  - `ADMIN_API_TOKEN`
  - `ADMIN_SECRET`
- Send the token as `X-Walmart-Trends-Admin-Token: <token>` or `Authorization: Bearer <token>`.
- Optional weekly Impact configuration:
  - `IMPACT_WALMART_CAMPAIGN_ID` defaults to `16662`.
  - `IMPACT_WALMART_PERFORMANCE_ENDPOINT` can override the account-specific product performance report endpoint. This endpoint must be validated against the configured Impact account/report.

## What is reused

- Walmart product lookup reuses `product_api.WalmartAPI`.
- Impact Walmart affiliate links reuse `product_api.ImpactAPI`.
- URLGenius link creation and registry dedupe reuse `product_api.URLGeniusAPI`.
- The page follows the existing Flask/Jinja template pattern and does not introduce a new frontend framework.

## Data model

The workflow creates normalized Walmart-specific tables during `db_schema.bootstrap()`:

- `walmart_products`
- `walmart_product_performance_snapshots`
- `walmart_affiliate_links`
- `walmart_urlgenius_links`
- `walmart_collections`
- `walmart_collection_items`
- `walmart_refresh_runs`

Each refresh run tracks source type, source window, source file, processed counts, failures, status, and timestamps.

## Workbook bootstrap

Run the initial workbook bootstrap with either endpoint or CLI:

```bash
python scripts/refresh_walmart_trends.py --mode bootstrap --workbook attached_assets/Walmart_May6th_Analysis.xlsx
```

or:

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

It creates:

- One `Top Sellers` collection by combining `1A` and `1B`, deduping by SKU, and preserving `Top by Units` / `Top by Earnings` badges.
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

1. Pulls the latest 7-day Impact product performance window. The adapter logs the endpoint, date window, raw row count, rows with SKUs, skipped rows, and rows missing recognizable performance fields.
2. Aggregates rows by SKU.
3. Calculates item count, sale amount, and total earnings.
4. Builds top 10 by item count with earnings tiebreaker.
5. Builds top 10 by earnings with item count tiebreaker.
6. Builds category-led themed collections, targeting 8–10 products.
7. Enriches products from Walmart.
8. Generates or reuses Impact links.
9. Generates or reuses URLGenius links.
10. Replaces active page collections idempotently.

## Fallback behavior

- If Walmart product enrichment fails, workbook or Impact performance values remain available and the product is marked with fallback enrichment status.
- If Impact link generation fails, the workflow falls back to the best known Walmart product URL.
- If URLGenius is unavailable or `URLGENIUS_API_KEY` is missing, the Impact URL is used directly and recorded as a fallback URLGenius row.
- Refresh failures are stored in `walmart_refresh_runs.failures_json`; a partial run can still publish usable collections.
- Failed weekly Impact runs do not deactivate existing active collections, so the public page continues showing the last successful or partial published collection set.
- Refresh runs use a SQLite guard to prevent overlapping bootstrap/weekly refreshes. A fresh `running` run blocks new refreshes; stale running rows older than two hours are marked failed before a new run starts.

## Landing page

- Public mobile page: `/walmart/trending-now`
- JSON data source: `/api/walmart/trending-now`

The page renders:

- Title: “What’s Trending Now”
- Last refreshed timestamp
- Top Sellers section first
- One horizontal product rail per curated collection
- URLGenius CTA links when available, with Impact/Walmart fallback links
- Empty states when no active collections exist

## Cron/Replit scheduled deployment

Configure a weekly scheduled command similar to:

```bash
python scripts/refresh_walmart_trends.py --mode weekly
```

For first-time setup or reseeding from the workbook, run bootstrap manually before enabling the weekly schedule.
