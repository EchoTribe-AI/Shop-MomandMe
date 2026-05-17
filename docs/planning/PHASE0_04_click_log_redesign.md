# Phase 0.4 — click_log Redesign + Event Contract

## Context

Today `click_log(id, asin, slug, fbclid, attribution_url, clicked_at)` captures one anonymous row per storefront click and bumps `collages.click_count`. The write path is an unauthenticated `POST /archer/track_click` (app.py ~3228) called from `templates/shop_landing.html` ~311 and listed as a passthrough in `_route_shop_subdomain` (app.py ~123). Phase 1.7 (UTM map) and 1.8 (GA4) require richer per-click context — source/medium/campaign, creator scoping, referrer — and storefront chat (`storefront_chat.py:245`) plus `insights.py` already read from this table. The endpoint must stay unauthenticated (anonymous shoppers) while gaining defenses the current code lacks: no rate limiting, no body validation, no bot guard, and a single field (`slug`) drives a free `UPDATE collages.click_count`.

## Approach

### Audit summary

Present today: `asin, slug, fbclid, attribution_url, clicked_at`. Missing: creator scoping (blocks multi-creator reads upstream), retailer, UTM tuple, referrer, user-agent, region, bot-flag, dedupe key. No indexes beyond the implicit PK — `insights.py` already scans by slug + time window. Side-effect `UPDATE collages` runs unconditionally — a curl loop inflates it.

### New schema (shared upstream, in `db_schema.init_schema()`)

```sql
CREATE TABLE IF NOT EXISTS click_log (
    id              BIGSERIAL PRIMARY KEY,
    creator_id      TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    collection_slug TEXT,                          -- was `slug`; keep name `slug` as alias view
    asin            TEXT,                          -- retailer-native id (asin OR walmart sku)
    retailer        TEXT,                          -- 'amazon' | 'walmart' | NULL (legacy)
    attribution_url TEXT,                          -- URLGenius / native affiliate URL
    fbclid          TEXT,
    utm_source      TEXT,
    utm_medium      TEXT,
    utm_campaign    TEXT,
    utm_content     TEXT,
    utm_term        TEXT,
    referrer        TEXT,                          -- document.referrer, trimmed
    user_agent      TEXT,                          -- raw UA, capped at 512 chars
    region_code     TEXT,                          -- ISO country (+ subdivision when available)
    client_id       TEXT,                          -- rotating storefront cookie / GA4 cid
    session_id      TEXT,                          -- 30-min storefront session
    bot_flag        TEXT,                          -- NULL=clean, else reason ('ua','rate','format')
    dedupe_key      TEXT,                          -- sha256(client_id|asin|slug|minute_bucket)
    clicked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Index strategy (read = creator + slug + time window; write = single insert):

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_click_log_creator_time
    ON click_log (creator_id, clicked_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_click_log_creator_slug_time
    ON click_log (creator_id, collection_slug, clicked_at DESC)
    WHERE collection_slug IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_click_log_creator_asin_time
    ON click_log (creator_id, asin, clicked_at DESC)
    WHERE asin IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_click_log_dedupe_recent
    ON click_log (dedupe_key, clicked_at DESC)
    WHERE dedupe_key IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_click_log_bot_flag
    ON click_log (bot_flag) WHERE bot_flag IS NOT NULL;
```

`bot_flag` rows are kept (not dropped) so they can be excluded from insights aggregates while remaining auditable. NULL is the clean path so the partial index stays small.

### Migration approach

New columns land via `_add_column_if_missing` per `db_schema.py:273` (SQLite path can't parse `ADD COLUMN IF NOT EXISTS`). Order: add each NULL-tolerated column, then rename via view (`CREATE OR REPLACE VIEW click_log_legacy AS SELECT id, asin, collection_slug AS slug, fbclid, attribution_url, clicked_at FROM click_log`) so unscoped legacy callers in `insights.py` / `storefront_chat.py` stay green during the cherry-pick window. Backfill `creator_id` from `collages` (`UPDATE click_log cl SET creator_id = c.creator_id FROM collages c WHERE cl.collection_slug = c.slug AND cl.creator_id IS NULL`) inside `init_schema`, behind a one-shot `cache_meta` flag. Indexes use `CREATE INDEX CONCURRENTLY` on PG (skipped on SQLite dev path). `BIGSERIAL` only on fresh installs — existing `SERIAL` keeps working; promotion is a separate ops task.

### Event contract (storefront JS → `POST /api/clicks`)

P0.1 owns the URL rename; this plan uses the renamed path. Content-Type `application/json`, body capped at 4 KB:

```json
{
  "v": 1,
  "creator_id": "everydaywithsteph",
  "collection_slug": "walmart-backyard-fun",
  "asin": "B0CLRNS4DD",
  "retailer": "amazon",
  "attribution_url": "https://urlgeni.us/amazon/HffbbK",
  "fbclid": "IwAR…",
  "utm": {
    "source": "facebook",
    "medium": "social",
    "campaign": "backyard-fun-may",
    "content": "card-3",
    "term": null
  },
  "referrer": "https://l.facebook.com/",
  "client_id": "1827.bd91…",
  "session_id": "s.4f3a…",
  "ts_client_ms": 1763398200123
}
```

Required: `v`, `creator_id`, one of (`asin` or `collection_slug`). Optional: everything else. Server fills `user_agent`, `region_code`, `clicked_at`, `bot_flag`, `dedupe_key`. UTM tuple maps 1:1 to GA4 `traffic_source` dimensions; same fetch can mirror to GA4 Measurement Protocol once P1.8 lands (no schema change required). The URL-canonical UTM source is the `attribution_url` query string when present — the client may omit `utm.*` and the server parses them out of `attribution_url` server-side (single source of truth).

### Server-side validation

`asin` ≤ 32 chars, `[A-Za-z0-9_-]`; `collection_slug` ≤ 80 chars, slug regex matches existing `collection_service.normalize_slug`; `creator_id` must exist in `creators` (cached lookup); `retailer` in `{amazon, walmart, null}`; `fbclid` ≤ 256 chars; UTM values ≤ 100 chars; `referrer` ≤ 512 chars; `user_agent` truncated server-side to 512; reject body > 4 KB with 413. Any failure → `bot_flag='format'`, write row, return 204 (do **not** echo why — minimize abuser feedback).

### Rate limiting & bot defense

In-process token bucket keyed by `(ip, creator_id)` at 30 req/min and `(client_id, collection_slug)` at 10 req/min. Over-budget rows still INSERT with `bot_flag='rate'` but **skip** the `collages.click_count` increment — this neutralizes the inflation vector. UA denylist regex (`curl|wget|python-requests|HeadlessChrome|bot|crawler|spider`) → `bot_flag='ua'`, also skip increment. No separate `click_attempts` table; one row per event with `bot_flag` is enough and simpler. Dedupe: same `dedupe_key` within 60s = `bot_flag='dupe'`, skip increment. The `collages.click_count` UPDATE runs only when `bot_flag IS NULL`.

### PII / region derivation policy

Do **not** store raw IP. Derive `region_code` at request time via `request.headers.get('CF-IPCountry')` (Cloud Run gives it via Cloudflare) or `request.access_route[0]` → MaxMind GeoLite2 country lookup, then discard the IP. Rate-limit keys use a SHA-256(IP + per-day salt) hashed in-memory, never persisted. `user_agent` is PII-borderline but operationally needed for bot defense — kept and reviewed at P3.4 (FTC/privacy doc).

### GA4 / UTM forward compatibility

JSON keys mirror GA4 reserved names so the same body can shim into Measurement Protocol with a trivial wrapper. P1.7 builder (`build_utm_params`) writes to the URLGenius `attribution_url` query string; server parser is the canonical reader. P1.8 adds a server-side fan-out to GA4 keyed on `creator_id` — no further schema change required.

## Files affected

- `db_schema.py` — **shared (upstream)**: new columns via `_add_column_if_missing`, indexes, backfill once-flag.
- `app.py` — **shared (upstream)**: rewrite `archer_track_click` (renamed by P0.1 to `/api/clicks`) with validation, rate-limit, region derivation, bot-flag write path; passthrough rename in `_route_shop_subdomain`.
- `templates/shop_landing.html` — **shared (upstream)**: JS payload upgraded to v1 contract; reads `client_id` from cookie, builds `utm` from `URLSearchParams`.
- `insights.py` — **shared (upstream)**: queries scope by `creator_id`, exclude `bot_flag IS NOT NULL`.
- `storefront_chat.py` — **shared (upstream)**: same scoping update.
- `product_api.py` — **shared (upstream)**: remove duplicate legacy `click_log` DDL at line 747 (single source = `db_schema.py`).
- `tests/test_click_log_redesign.py` — **shared (upstream)**: new.

## Verification

- Schema migration: boot against a copy of `prod_seed.sql`; confirm columns added, indexes present (`\d+ click_log`), and legacy rows have `creator_id` backfilled from `collages`.
- Smoke: `curl` the new endpoint with a full v1 body → 204, row visible with all fields, `collages.click_count` incremented exactly once.
- Bot flood: 200 req/min from one IP → first 30 clean, remainder `bot_flag='rate'`, `click_count` unchanged after request 30. UA `python-requests/2.31` → `bot_flag='ua'`, no increment. Dupe within 60s → `bot_flag='dupe'`.
- Validation: oversized body → 413; bad slug → row written with `bot_flag='format'`; unknown `creator_id` → 204 with `bot_flag='format'`, no increment.
- GA4 mappability: stub Measurement Protocol receiver consumes the JSON body verbatim and resolves `utm_*` into expected GA4 dimensions.
- Read scoping: `insights.py` against a two-creator fixture returns only the active creator's rows; bot-flagged rows excluded.

## Open questions

- Region source on Replit Cloud Run: confirm whether `CF-IPCountry` is present or whether GeoLite2 needs vendoring (ops decision; affects whether `region_code` is best-effort vs reliable).
- Coordinate with P0.5 (Security Engineer) on whether the in-process token bucket suffices or we need a Redis/PG-backed counter for multi-worker safety — current `gunicorn --workers=1` makes in-process safe today, but P0.5 may raise workers.
- Legal review of `user_agent` retention window before P3.4 (90-day default proposed; needs sign-off).
- Confirm P0.3 attribution: `click_log` stays anonymous (no `*_by_user_id` columns) — but admin-side click reports join `collages.created_by_user_id` to credit the editor. Document explicitly in `insights.py` rebuild plan.
- `client_id` cookie scope: first-party `shop.echotribe.ai` vs `.echotribe.ai` — affects cross-subdomain attribution if a future creator hub spans subdomains.
