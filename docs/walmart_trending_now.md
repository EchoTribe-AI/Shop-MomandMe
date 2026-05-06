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
- URLGenius link creation and registry dedupe reuse `product_api.URLGeniusAPI`.
- `product_api.ImpactAPI` is reserved for later weekly reporting/affiliate phases and is not used by workbook bootstrap.
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
python scripts/refresh_walmart_trends.py --mode bootstrap --workbook attached_assets/Walmart_May6th_Analysis.xlsx --link-mode urlgenius
```

or:

```bash
curl -X POST http://localhost:5000/admin/walmart-trends/bootstrap \
  -H 'Content-Type: application/json' \
  -H 'X-Walmart-Trends-Admin-Token: <token>' \
  -d '{"workbook":"attached_assets/Walmart_May6th_Analysis.xlsx","link_mode":"urlgenius"}'
```

Phase 1 workbook bootstrap is data/page population plus URLGenius-only CTA wrapping. It never calls Impact, the Impact ConversionLink endpoint, Impact for product details, weekly Impact reporting, or Impact affiliate-link creation. With the default `--link-mode urlgenius`, each CTA is generated/reused from the canonical Walmart URL (`https://www.walmart.com/ip/{sku}`) through URLGenius and falls back to that canonical Walmart URL. Use `--link-mode workbook-only` or the legacy `--skip-links` flag for zero external calls.

The bootstrap parser reads:

- `Trending - Item Count First` as source list `1A`
- `Trending - Earnings First` as source list `1B`
- `Curated Collections` as collection rows

The CLI output includes workbook diagnostics: workbook path, sheet names found, parsed record counts, curated collection names, inserted/updated product and collection counts, active collection count, first active collection slug, first three SKUs in that collection, `link_mode`, `link_generation_skipped`, `impact_calls_made`, `urlgenius_calls_made`, `urlgenius_links_created_or_reused`, and `fallback_canonical_links_used`.

It creates:

- One `Trending Now` collection by combining `1A` and `1B`, deduping by SKU, and preserving `Popular Pick` / `Trending Deal` / `Hot Find` badges.
- One section for each workbook curated collection.

Diagnostic command for checking the first 10 API CTA URLs:

```bash
uv run python - <<'PY'
from app import app
with app.test_client() as c:
    data = c.get('/api/walmart/trending-now').get_json()
    urls = [p['shop_url'] for col in data.get('collections', []) for p in col.get('items', [])]
    for url in urls[:10]:
        print(url)
PY
```

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
- Phase 1 workbook bootstrap never calls Impact.
- In `urlgenius` mode, URLGenius failures fall back to canonical Walmart URLs and do not block page population.
- In `workbook-only` mode, workbook bootstrap skips external link calls entirely.
- Refresh failures are stored in `walmart_refresh_runs.failures_json`; a partial run can still publish usable collections.
- Failed weekly Impact runs do not deactivate existing active collections, so the public page continues showing the last successful or partial published collection set.
- Refresh runs use a SQLite guard to prevent overlapping bootstrap/weekly refreshes. A fresh `running` run blocks new refreshes; stale running rows older than two hours are marked failed before a new run starts.

## Landing page

- Public mobile page: `/walmart/trending-now`
- JSON data source: `/api/walmart/trending-now`

The page renders:

- Title: “What’s Trending Now”
- Last refreshed timestamp
- Trending Now section first
- One horizontal product rail per curated collection
- Phase 1 CTA links point to URLGenius links when available, otherwise canonical Walmart product URLs
- Empty states when no active collections exist

## Cron/Replit scheduled deployment

Configure a weekly scheduled command similar to:

```bash
python scripts/refresh_walmart_trends.py --mode weekly
```

For first-time setup or reseeding from the workbook, run bootstrap manually with `--link-mode urlgenius` before enabling any later scheduled workflow.
