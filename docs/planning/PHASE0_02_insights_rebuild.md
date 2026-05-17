# Phase 0.2 — Insights Rebuild (Creator-Facing Framework)

## Context

The current `insights.py` and `templates/insights.html` were built for EchoTribe internal ad-ops — paid-attribution reconciliation, Archer label rollups, click-weighted earnings distribution. Wrong audience and wrong shape for P0.2. Per the 2026-05-17 realignment, `/insights` is **shared upstream framework code** that renders a creator-scoped analytics dashboard. Echo-Dashboard serves it against the demo creator on `shop.echotribe.ai/insights`; Shop-MomandMe serves the same templates against the `everydaywithsteph` row on `shop.mommyandmecollective.com/insights`. The "View Insights" button at the bottom of every collection page lands here. The original module gets rewritten in place — kept on disk so the strip-down PR doesn't churn it, but the contents are new.

## Approach

### Page structure
Single mobile-first dashboard at `/insights` (session-gated via `@require_admin_page` — no URL tokens). Three vertically stacked zones on mobile, all driven by `creator_id`:

1. **Header strip** — top-line numbers for the selected window. Total clicks is safe to seed; **[NEEDS STEPH INPUT]** on whether earnings, conversion rate, top retailer, or "best collection today" deserve the prime real estate. We will ship the strip with three slots and a placeholder until Steph reviews a draft.
2. **Collection performance list** — one row per published collection, sorted by clicks desc. Each row: title, click count, last-click recency, theme color chip, mini sparkline. **[NEEDS STEPH INPUT]** on secondary metric (earnings estimate? CVR? retailer mix?) and on whether collection-level is even the right unit, vs. product-level or retailer-level.
3. **Detail drawer** — tapping a collection row opens a per-collection view: products inside, clicks per product, outbound retailer split. **[NEEDS STEPH INPUT]** on whether she wants forecast/projection, comparison-to-prior-period framing, or strictly historical.

A subroute `/insights/category/<name>` is reserved in the feature list — we stub the route, defer the view.

### Query architecture
New `insights.py` exposes pure functions, all taking `creator_id` as the first arg:
- `overview(creator_id, start, end) -> dict`
- `collections_summary(creator_id, start, end) -> list[dict]`
- `collection_detail(creator_id, slug, start, end) -> dict`

All queries scope by `COALESCE(creator_id, 'everydaywithsteph') = ?` on `collages` and `posts` and (post-P0.4) on `click_log`. Connections via `db_schema._connect()`. SQL paramstyle-agnostic so PG and the SQLite dev fallback both work. No `bootstrap()` on the hot path.

### `creator_id` scoping
Route resolves the active creator the same way P0.7 framework resolution does: subdomain → session → env default. The same template renders for any creator; no creator name is hardcoded.

### Time-window defaults
Window selector ships with: Today, 7d, 30d, Custom. Default landing window: **[NEEDS STEPH INPUT]** — recommend 7d on mobile to match her cadence, but defer. Comparison framing (vs. prior period?) **[NEEDS STEPH INPUT]**.

### click_log dependency (coordinate with P0.4)
What the dashboard needs from `click_log`: `creator_id`, `slug`, `asin`, `retailer`, `clicked_at`, `source` (organic vs. paid vs. external referrer). Current schema is minimal and writes are unauthenticated. **P0.4 must land first** — its new schema is the authoritative event contract this dashboard reads. Until P0.4 lands, the rebuild can stub against existing fields (`slug`, `asin`, `clicked_at`) with future-extension comments where the richer fields slot in.

### GA4 / UTM (coordinate with P1.7/P1.8)
Phase 0 reads `click_log` only. GA4 data integration is a Phase 1 follow-on; leave a clearly named module seam (`insights_ga4.py` not created yet) for it.

### What we can build now without Steph input
- The route, auth wiring, `creator_id` resolution, query plumbing, page skeleton, mobile-first CSS scaffold per the framework's design-system variables, the window selector mechanism, the empty/loading/error states, the "View in Insights" button wiring from the collection page.

### What we seed and revise
- Metric choices in the header strip, sort order in the collection list, default window, color thresholds for "doing well" badges, presence/absence of comparison framing. We seed reasonable defaults, ship a draft for Steph, revise.

## Files affected

- `/insights.py` — **shared (upstream).** Rewritten. Old EchoTribe ad-ops functions removed.
- `/templates/insights.html` — **shared (upstream).** Rewritten. Old EchoTribe tab structure removed.
- `/templates/partials/insights_*.html` *(new, as needed)* — **shared (upstream).** Header strip, collection row, detail drawer partials.
- `/app.py` — **shared (upstream).** `/insights` route swapped to `@require_admin_page`, `creator_id` resolved from framework context, calls new `insights.py` functions, removes references to deleted Archer attribution helpers.
- `/templates/walmart_collection_create_post.html` — **shared (upstream).** "View in Insights" link already exists; verify it points at the rebuilt page.
- `/static/css/*` (or framework design-system file per P0.7) — **shared (upstream).** Insights-specific styles.
- `/tests/test_insights_framework.py` *(new)* — **shared (upstream).** `creator_id` scoping, empty-state, auth gate, query smoke.
- `/branding/` — **client-only.** No change required; brand chrome inherits from framework.

## Verification

1. `python3 -m unittest discover -s tests` — full suite green; new `test_insights_framework.py` covers `creator_id` isolation, auth gate, empty-state.
2. Auth smoke: unauthenticated `GET /insights` → 302 to `/admin/login`. Authed → 200.
3. Two-creator smoke on Echo-Dashboard: seed a second creator row, set `creator_id` via env, confirm only that creator's collections appear.
4. Mobile visual review: open on iPhone-sized viewport; confirm single-column flow, no horizontal scroll, tap targets ≥ 44px.
5. Downstream sync smoke: cherry-pick to Shop-MomandMe, verify `/insights` renders the `everydaywithsteph` data with Mommy & Me branding via `branding/` override.
6. **Steph review pass:** screen-share the draft, walk through, capture answers to every `[NEEDS STEPH INPUT]` flag below. Revise before merge.

## Open questions

| # | Question | Owner | Why it matters |
|---|---|---|---|
| 1 | Which metric belongs in the header strip's prime slots — earnings? CVR? top retailer? best collection today? | Kelly → Steph | Drives what the page is "about" at a glance. Wrong choice = ignored dashboard. |
| 2 | Is the primary unit collection-level, product-level, or retailer-level? | Kelly → Steph | Reorders the entire page. Tech is identical; presentation isn't. |
| 3 | Default landing time window (Today / 7d / 30d)? | Kelly → Steph | Affects how "alive" the page feels on first load. |
| 4 | Comparison framing — vs. last period, vs. last month, or no comparison? | Kelly → Steph | Adds a column and a query; cheap to add, distracting if unwanted. |
| 5 | Forecast/projection vs. strictly historical? | Kelly → Steph | Future-tier signal; we don't build it now, but we want her direction. |
| 6 | What "doing well" looks like visually — color thresholds, badge logic? | Kelly → Steph | Drives the design-system tokens for status colors. |
| 7 | Secondary metric beside click count in the collection list (earnings est., CVR, retailer mix)? | Kelly → Steph | Defines the second column of the most-used view. |
| 8 | P0.4 click_log schema — what fields are guaranteed by the time P0.2 ships? | Database Optimizer (P0.4 plan) | Blocks: P0.2 reads from `click_log`. If P0.4 slips, P0.2 ships against existing fields with stubs. |
| 9 | Does the demo creator on Echo-Dashboard have enough seeded click data to be a useful preview surface? | Kelly | If sparse, build with empty-state-first mindset. |
| 10 | Should `/insights` be linked from anywhere besides the collection page bottom button (e.g., admin hub)? | Kelly → Steph | Nav surface decision; trivial once answered. |
