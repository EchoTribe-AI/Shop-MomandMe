# EchoTribe Dashboard — Production Source of Truth

> **Live in production on Replit since the `codex/fresh-pg-launch` deploy.**
> This doc is the canonical operational reference for the running system.
> Anything older (earlier runbooks, plan files, slack threads) is stale.

---

## Status

| | |
|---|---|
| **Deployed branch** | `main` (PR #37 merged 2026-05-17; baseline tagged `v1.0-pg-launch`) |
| **Deployed commit** | `<current main HEAD>` — update this line per deploy via `git log --oneline -1 origin/main` |
| **Database** | Replit managed PostgreSQL (auto-injected `DATABASE_URL`) |
| **Public site** | `https://shop.echotribe.ai` |
| **Admin entry** | `/admin/login` — `ADMIN_PASSWORD` env var required in production (no default) |
| **Health probe** | `GET /healthz` → `200 ok` (no DB touch, no auth) |
| **Tests** | `python3 -m unittest discover -s tests` — expected 301 passing, 1 skipped |
| **Production fail-closed posture** | Missing `SECRET_KEY` or `ADMIN_PASSWORD` → admin paths return 503 with a clear message; `/healthz` and public storefront stay up. Loud `[BOOT] PRODUCTION ADMIN CONFIG MISSING` line logged at module import. |

---

## The single source of truth

**Production data lives in Replit's managed PostgreSQL database. Nothing else.**

- `data/archer_catalog.db` is **dev-only** SQLite fallback for local development when `DATABASE_URL` is unset. It is **`.gitignore`d** and must never be committed.
- `scripts/prod_seed.sql` (~5,081 lines, 2026-05-15 snapshot) is a **last-resort recovery file**, not a re-sync source. Prefer re-importing fresh workbooks if data needs rebuilding.
- `scripts/migrate_sqlite_to_postgres.py` is an **explicit one-shot tool** for re-doing the initial dev→prod migration. It does NOT run at app boot. Do not call `db_schema.bootstrap()` from it; use `init_schema()` + `seed_default_creator()` directly.

If you need to know what's "really" in production, query Replit PG. Don't infer from any file.

### Repo artifact classification (audit follow-up 0.6)

Codex audit flagged several large files committed to the repo. Each was reviewed via `git log -- <path>` and `git grep` to confirm code dependencies. Classification:

| Path | Status | Rationale |
|---|---|---|
| `data/Archer Full Catalog 2026.csv` (~23 MB) | **KEEP** | Active source — read by `product_lookup_service.py:12` (`CATALOG_PATH`) and `product_api.py:552`. ArcherAPI's local catalog is itself a KEEP feature per the deferred-work list. |
| `data/earnings_latest.csv` (~64 KB) | **KEEP (sensitive)** | Active source — read by `product_api.py:1009` (`EARNINGS_CSV_PATH`) for the 586-ASIN earnings dataset. Contains real ASIN-level click/revenue data; not a security risk in the strictest sense but should be treated as sensitive. Already implicitly committed history; rotation to git-LFS or `.gitignore`d storage is a future cleanup, not blocking. |
| `data/urlgenius_registry.backup-pre-april-20260430-150530.json` (~4.9 MB) | **KEEP** | Intentional point-in-time recovery snapshot from 2026-04-30. Not referenced by any code; pure backup. Retained for rollback if the live `urlgenius_registry.json` becomes corrupt. |
| `scripts/prod_seed.sql` (~3.2 MB) | **KEEP** | Documented above as last-resort recovery. Active recovery path; do not remove. |
| `attached_assets/*.xlsx` (Walmart/Amazon analysis workbooks) | **KEEP** | Required by the workbook import flow — `walmart_trends.py:28` (`DEFAULT_WORKBOOK`), `app.py:2395` (`/admin/walmart-trends/bootstrap`), `tests/test_workbook_import.py:23`. Replace by re-importing newer workbooks; do not delete blindly. |
| `attached_assets/Pasted-*.txt`, `attached_assets/app_*.py`, `attached_assets/index_*.html`, `attached_assets/steph-*.html`, `attached_assets/MMC-Logo.png`, `attached_assets/image_*.png`, `attached_assets/targeted_element_*.png` | **CLEANUP CANDIDATE** | Replit-IDE conversation paste artifacts auto-committed during agent sessions. Not referenced by any code (confirmed via `git grep`). Should be removed in a follow-up `chore/cleanup-attached-assets` PR — not in this hardening PR because that would obscure the security-fix diff. The `Pasted--i-noticed-its-stored-in-secrets-like-this-...` file was reviewed; it contains a discussion of PEM-key escaping with the literal `xxxxx=` placeholder, not a real secret. |
| `attached_assets/URLgenius_API_Documentation_*.md`, `attached_assets/CHAT_PRODUCT_CARDS_GUIDE_*.md`, `attached_assets/FINAL_SUMMARY_*.md`, `attached_assets/INTEGRATION_STEPS_*.md` | **CLEANUP CANDIDATE** | Same as above — IDE paste artifacts. If any of these are genuinely the only copy of useful documentation, lift them into `docs/` first, then delete from `attached_assets/`. |
| `attached_assets/urlgenius_updated_registry_with_clicks.json` | **CLEANUP CANDIDATE** | Another registry backup; superseded by `data/urlgenius_registry.backup-pre-april-...json`. Confirm before removal. |

Repo-history cleanup (removing large files from past commits via `git filter-repo` / BFG) is intentionally out of scope here — that requires a force-push to `main` which would invalidate every contributor's local clone. If it becomes necessary (e.g., the repo grows too large for Replit clone times), schedule it as a one-shot maintenance window.

---

## The `DATABASE_URL` gotcha (the most important operational lesson)

**Replit's managed PostgreSQL service auto-injects `DATABASE_URL` (and `PGHOST` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` / `PGPORT`) into the deployment environment whenever the PG service is provisioned to a Repl.**

If you ALSO set `DATABASE_URL` manually under Replit Secrets, the manual value shadows the auto-injected one. The app can silently connect to the wrong database (or fail to bind, producing the 6+ second startup → Cloud Run health-check timeout the team hit on the first deploy).

**Rule**: Do **not** set `DATABASE_URL` manually in Replit Secrets. Let the PG service inject it.

Verify which DB the app is connected to:

```bash
# In the Replit shell of the deployment:
env | grep -E 'DATABASE|PG' | sort
psql "$DATABASE_URL" -c "SELECT current_database(), inet_server_addr(), COUNT(*) FROM walmart_products;"
```

`bootstrap()` logs the initialization time on success:
```
[BOOT] schema ready in 0.09s
```

---

## Environment variables

| Variable | Source | Required | Notes |
|---|---|---|---|
| `DATABASE_URL` | **Replit-managed** (auto-injected) | yes (in prod) | Do NOT set manually. Unset = SQLite fallback (dev only). |
| `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGPORT` | Replit-managed | yes (in prod) | Same — handled by the PG service. |
| `SECRET_KEY` *(or `FLASK_SECRET_KEY`)* | User-defined Secret | **REQUIRED in prod** | Signs the Flask session cookie. Rotate before going wide; expires existing sessions. If missing while `DATABASE_URL` is set, every admin path returns 503 — see "Production fail-closed posture" above. |
| `ADMIN_PASSWORD` | User-defined Secret | **REQUIRED in prod** | Case-insensitive comparison. If missing while `DATABASE_URL` is set, every admin path returns 503. Dev mode (`FLASK_ENV=development` or no `DATABASE_URL`) falls back to `dan`. |
| `WALMART_TRENDS_ADMIN_TOKEN` | User-defined Secret | optional | Legacy admin API token; still accepted by `_require_walmart_trends_admin` alongside the session cookie for external automation. |
| `ANTHROPIC_API_KEY` | User-defined Secret | yes for chat/copy | Storefront chat + caption generation. |
| `WALMART_API_KEY`, `IMPACT_ACCOUNT_SID`, `IMPACT_AUTH_TOKEN`, `URLGENIUS_API_KEY` | User-defined Secret | yes for Walmart trends | Workbook import + affiliate link generation. |
| `CRAWLBASE_JS_TOKEN` | User-defined Secret | yes for Amazon enrichment | Crawlbase fallback when Creators API misses. |
| Amazon Creators API config (multiple vars) | User-defined Secret | optional | Primary Amazon enrichment path; see `utils/amazon_creators.py`. |
| `CACHE_DB_PATH` | env override | no | Only useful in local dev to point the SQLite fallback elsewhere. Ignored when `DATABASE_URL` is set. |
| `SHOP_SUBDOMAIN` | env override | no | Defaults to `shop.echotribe.ai`. |
| `FLASK_ENV`, `FLASK_DEBUG`, `REPLIT_DEV_DOMAIN` | env | no | Trigger `_walmart_content_demo_allowed()` (dev-only auth bypass). |

---

## Architecture quick-reference

- **`db_schema._PGConn`** — thin psycopg2 wrapper that mimics the sqlite3 `Connection.execute()` API so all call sites stay paramstyle-agnostic. Set in `_connect()` when `DATABASE_URL` is present; otherwise returns a real `sqlite3.Connection`.
- **`db_schema._adapt_sql`** — translates SQLite-flavored SQL on the way out: `?` → `%s`, `datetime('now', '±N units')` → PG `INTERVAL`, `BEGIN IMMEDIATE` → `SELECT 1` (no-op; PG is implicitly in a transaction).
- **`db_schema.bootstrap()`** — `init_schema()` + `seed_default_creator()`. Fast, sync, idempotent. **No data movement.**
- **`app._ensure_schema_ready()`** — lazy memoized schema check; runs at first admin route hit, not at module import. Boot is fast; Cloud Run's health probe (`/healthz`) returns instantly without ever touching the DB.
- **Admin auth** — server-side Flask session cookie (signed with `SECRET_KEY`, 30-day lifetime) issued by `/admin/login`. Admin pages redirect to login if unauthed. Admin API routes accept the session OR the legacy `X-Walmart-Trends-Admin-Token` header.
- **Gunicorn** — `--workers=1 --timeout=120` in `.replit`. Cloud Run scales via containers, not gunicorn workers; multiple workers caused DB connection contention with no upside.
- **`/healthz`** — public route, always returns `200 ok`, no DB query. Cloud Run health probe target.

---

## Per-creator framework (planning context — implementation lands in P0.7)

> **2026-05-17 architecture realignment.** The earlier split treated the storefront (`templates/shop_*`, `templates/walmart_*`, `/shop/`, `/collections/`, `/admin/login`, `templates/partials/`) as **client-only** — each creator deploy edited those files locally. That model didn't scale: improvements diverged across deploys instead of converging on a tested framework.
>
> The realigned model: **Echo-Dashboard hosts the storefront framework against a demo creator on `shop.echotribe.ai`.** The framework is shared upstream. Per-creator deploys (the first being Shop-MomandMe at `shop.mommyandmecollective.com`) override only what's genuinely creator-specific.

**What this means operationally:**

- **Echo-Dashboard's storefront is real.** Visitors to `https://shop.echotribe.ai/shop/`, `/collections/<slug>`, `/posts`, `/trends`, etc. see a working storefront rendered against the demo creator (`everydaywithsteph` row in the `creators` table). It is not a stub or a redirect — it is the framework running for development and QA.
- **Per-creator data lives in the `creators` table.** Existing columns (`display_name`, `handle`, `brand_label`, `voice_prompt`, `theme_default`, `defaults_json`) plus the additions defined in P0.7 (`logo_url`, `primary_color`, `accent_color`, `shop_domain`, `meta_title_template`, `meta_description_template`) carry the brand identity. Templates render against the active `creator_id` resolved from subdomain / session / env default — no creator name is hardcoded.
- **Per-deploy overrides live in a `branding/` directory** (new in P0.7) on each downstream deploy. Mommy & Me's logo, favicon, and any deploy-specific brand assets sit there. The framework reads `branding/` at render time when it exists and falls back to demo-creator defaults when it doesn't.
- **EchoTribe-internal admin (`/archer/*`, EchoBoost, Levanta, brand-side dashboards) stays on Echo-Dashboard only.** Shop-MomandMe's strip-down PR removes those routes; the storefront framework stays.
- **Improvements develop on Echo-Dashboard first.** A storefront template tweak gets coded and tested against the demo creator on `shop.echotribe.ai`. After merge to Echo-Dashboard `main`, the next cherry-pick sync pulls it into Shop-MomandMe, where Mommy & Me's branding overrides re-apply at render time.

**P0.7 (Phase 0 planning doc, Software Architect agent)** owns the technical spec: schema additions, branding-override read path, demo-creator seed, sync-friendly conflict semantics. Until P0.7 lands, treat this section as forward-looking architecture context, not current production behavior. The runbook will note the transition when P0.7 ships.

Cross-links:
- `docs/planning/PHASE0_07_storefront_framework_boundary.md` — P0.7 implementation plan. *This file ships in the Phase 0 planning PR; once that lands the path resolves directly. Until then the spec exists in that PR's diff.*
- Shop-MomandMe `docs/UPSTREAM_SYNC.md` — downstream-side shared-surface list (updated to match this realignment)

---

## Never do this (the lessons learned)

1. **Don't commit `data/archer_catalog.db`** (or any production data file) into git. It bloats the repo, ships stale data into every container, and PII-risks the history.
2. **Don't add auto-seed-from-snapshot logic** to `bootstrap()` or app import — neither synchronously NOR via a daemon thread. The migration script is the only data-movement path.
3. **Don't set `DATABASE_URL` manually** in Replit Secrets when the managed PG service is provisioned. Let Replit inject it.
4. **Don't embed `WALMART_TRENDS_ADMIN_TOKEN`** into rendered HTML or browser JavaScript. Use session auth, or send the header from server-trusted code only.
   - As of audit follow-ups 0.2 / 0.3 (PR title: "Pre-launch hardening (audit follow-ups)"), `?admin_token=` URL query-string authentication is fully removed from `_require_walmart_trends_admin()` and `_require_admin_page()`, and the hardcoded `?token=SEED_MMC_2026` parameter on `/admin/seed-production` is gone. Only the `X-Walmart-Trends-Admin-Token` header / `Authorization: Bearer <token>` are accepted for header-based auth. Do not reintroduce URL token auth — query strings leak through proxy logs, browser history, and Referer headers.
5. **Don't call `db_schema.bootstrap()` from hot read paths** (e.g., `get_trending_page_data`, public storefront routes). Use `_ensure_schema_ready()` on admin routes; public routes don't need it once the app is warm.
6. **Don't use SQLite-only patterns** in new code:
   - ❌ `cursor.lastrowid` → ✅ `INSERT ... RETURNING id` + `db_schema._last_id(cur)`
   - ❌ `INSERT OR REPLACE` → ✅ `INSERT ... ON CONFLICT (...) DO UPDATE`
   - ❌ raw `sqlite3.connect(...)` → ✅ `db_schema._connect()`
   - ❌ `datetime('now', '-2 hours')` → `_adapt_sql` translates this transparently, but prefer native PG `NOW() - INTERVAL '2 hours'` in new code
7. **Don't add `.replit.app` back to `_walmart_content_demo_allowed()`** — that previously auto-granted admin to every visitor on production Replit URLs. The function intentionally allows only true dev hosts (`localhost`, `.replit.dev`, `.repl.co`).

---

## Common operations

### Sign in
- Visit any admin page (`/hub`, `/walmart/trending-now?admin=1`, etc.).
- Redirected to `/admin/login` if not authed.
- Enter `ADMIN_PASSWORD` (case-insensitive). In production this MUST be set as an env var — no default. Locally (no `DATABASE_URL`) it defaults to `dan`.
- 30-day session cookie issued. Sign out: `/admin/logout`.
- If you see "Admin unavailable: missing production config (SECRET_KEY|ADMIN_PASSWORD)" on `/admin/login`, the deploy is missing required env vars — set them in Replit Secrets and redeploy. `/healthz` and the public storefront continue working in the meantime.

### Import a workbook
- `/walmart/trending-now?admin=1` → **Workbook Import** dropdown.
- Pick a `Walmart_*.xlsx` or `Amazon_*.xlsx` file from `attached_assets/`.
- Click Run.
- **Amazon imports auto-trigger** an enrichment pass (limit 30, max_workers 4) right after import.

### Re-enrich Amazon prices / images
- Same admin page → **"Enrich Amazon prices/images"** button.
- Posts to `/admin/amazon-trends/enrich` with `{limit: 30, max_workers: 4}`.
- Response includes a `missing_rows` counter. **If non-zero, those ASINs need to be (re-)imported via workbook**, not just enriched. (Counter introduced in `ba9a8a7` to surface the "API said success but the row didn't exist" class of bug.)
- Bump the limit by POSTing directly: `curl -X POST $URL/admin/amazon-trends/enrich -d '{"limit": 200, "max_workers": 4}'`.

### Edit a published collection
- `/collections/<slug>/edit` — loads in <1 second (was 6+ seconds before commit `ba9a8a7`; fixed by `WalmartTrendStore.get_collection_by_slug()` which scopes the query to one collection instead of loading all 31).
- Mobile-first editor: summary/status card → single primary save action → quick actions → publishing card → content below → collapsed secondary tools.
- Publish / unpublish / archive flows persist to PG and survive refresh.

### Recover data
- **Preferred**: re-import the latest workbook(s). Always produces fresh data.
- **Last resort**: POST `/admin/seed-production`. Runs `scripts/prod_seed.sql` (2026-05-15 snapshot). **Data will be stale.** Don't use unless re-import is impossible.

### Run the SQLite → PG migration manually
Only needed if you're re-provisioning a fresh PG environment from a SQLite snapshot.

```bash
export DATABASE_URL='postgresql://...'
export CACHE_DB_PATH='/path/to/sqlite-snapshot.db'
python3 scripts/migrate_sqlite_to_postgres.py
```

The script uses `init_schema()` + `seed_default_creator()` directly (not `bootstrap()`), per-row savepoints (a bad row never rolls back the batch), and resets all `SERIAL` sequences to `MAX(id)+1` at the end so future inserts don't collide.

---

## Admin endpoint reference

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/healthz` | public | Returns `200 ok`. No DB. Cloud Run health probe. |
| GET | `/admin/login`, POST | public | Single password form → session cookie. |
| GET | `/admin/logout` | session | Clears session. |
| GET | `/hub` | session | Admin home. |
| GET | `/walmart/trending-now?admin=1` | session when `admin=1` | Public without `?admin=1`. |
| POST | `/admin/walmart-trends/bootstrap` | session OR header token | Body: `{workbook: "/abs/path.xlsx"}`. |
| POST | `/admin/walmart-trends/refresh` | session OR header token | Pulls 7-day rolling Impact API report. |
| POST | `/admin/amazon-trends/bootstrap` | session OR header token | Auto-enriches after import. |
| POST | `/admin/amazon-trends/enrich` | session OR header token | Body: `{limit, max_workers}`. Returns counts incl. `missing_rows`. |
| POST | `/admin/seed-production` | session OR header token | Runs `prod_seed.sql`. **Last-resort recovery only.** |
| GET / POST | `/admin/creators`, GET `/admin/creators/<id>` | session | Creator management. |

---

## Important file locations + functions to reuse

| Path | Purpose |
|---|---|
| `db_schema._connect()` | Connection factory (PG via `_PGConn` or SQLite fallback). |
| `db_schema._adapt_sql()` | SQL translation chokepoint. |
| `db_schema._last_id(cur)` | Replaces `cursor.lastrowid`. |
| `db_schema.init_schema()`, `db_schema.seed_default_creator()` | Explicit boot helpers; safe to call individually. |
| `db_schema.bootstrap()` | The two above, in order. Safe to call repeatedly. |
| `walmart_trends.WalmartTrendStore.get_collection_by_slug(slug)` | Narrow path for one collection (use for create-post / editor flows). |
| `walmart_trends.WalmartTrendStore.landing_page_data()` | Full path for the public `/trends` page only. |
| `amazon_trends.AmazonTrendStore.update_product_enrichment(asin, data, status)` | Writes display fields including `product_title`. Returns int rowcount. |
| `amazon_trends.AmazonProductEnricher.enrich_batch(asins, max_workers)` | Concurrent enrichment. Returns counts incl. `missing_rows`. |
| `collection_service.normalize_slug()`, `publish_collage()` | Slug + publish helpers. |
| `collection_content.publish_latest_draft_for_public_slug()` | Draft → published page promotion. |
| `scripts/migrate_sqlite_to_postgres.py` | Manual one-shot SQLite → PG migration. **Not run at boot.** |
| `scripts/prod_seed.sql` | Last-resort recovery snapshot (2026-05-15). |
| `templates/admin_login.html` | Single-password login form. |
| `templates/partials/admin_header.html` | Admin nav (no `?admin_token=` URL params). |
| `tests/test_pg_compat_and_defaults.py` | Adapter translation, PG publish CASE, mommyme theme, smart-link campaign, narrow-query, fresh-PG-launch safety. |
| `tests/test_amazon_phase3.py` | Amazon enrichment (Creators + Crawlbase), product_title write, rowcount safety. |

---

## Rollback

If `codex/fresh-pg-launch` deployment breaks: switch the Replit deploy branch back to `codex/published-page-persistence`. That branch is still on SQLite and intact — the migration was read-only on the SQLite side, so no data loss is possible.

Note: rollback abandons any new data written to PG since the cutover, because `codex/published-page-persistence` writes to SQLite. If you need to keep the new PG data, fix forward instead of rolling back.

---

## Deferred work (next branch off `main` after one-week soak)

**Step 7 slim-down** — remove drop-list features that aren't shipping:

- Routes: `/archer/ads`, `/archer/ads/save`, `/archer/ads/campaigns`, `/archer/organic`, `/archer/products` (the page; the API stays — used by `/archer/collage`), `/archer/generate_link`, `/archer/generate_ad_copy`, `/insights`, `/archer/campaigns/*`, `/archer/discovery/top_clicked`, `/urlgenius`, `/urlgenius/smart_link`, `/urlgenius/create_link`, `/urlgenius/sync`, `/urlgenius/links`, `/archer/urlgenius`, `/levanta/*`, `/webhooks/levanta`.
- Modules: `campaign_builder.py`, `insights.py`, `link_builder.py`, `main.py`.
- Templates: `archer_ads.html`, `archer_products.html`, `archer_campaigns.html`, `insights.html`, `urlgenius_links.html`, `organic_posts.html`.
- `product_api.py` classes (conditional on `git grep`): `LevantaAPI`, `LevantaNetworkMatcher`, `ArcherNetworkMatcher`.

**Keep** despite earlier drop-list discussion:
- `/archer/search` — `/archer/collage` (KEEP) depends on it.
- `ArcherAPI` class — local cache layer used by all KEEP features (same `data/archer_catalog.db` file as `db_schema.DB_PATH` default).
- `URLGeniusAPI` — used by Walmart trends affiliate-link wrapping.

Every deletion gated by `git grep` first. Per-feature commit. Soak each pass before the next.

---

## Test commands

```bash
# Full suite
python3 -m unittest discover -s tests

# Specific suites
python3 -m unittest tests.test_pg_compat_and_defaults
python3 -m unittest tests.test_amazon_phase3
python3 -m unittest tests.test_walmart_storefront_cleanup

# Auth smoke against running app (DATABASE_URL set):
curl -i http://localhost:5000/healthz                                    # 200 ok
curl -i -L http://localhost:5000/hub                                     # 302 → /admin/login
curl -i -d "password=dan" http://localhost:5000/admin/login -c /tmp/c   # 302 → /hub
curl -i -b /tmp/c http://localhost:5000/hub                              # 200
```
