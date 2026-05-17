# Phase 0.6 — Slim-Down (Beyond Action 1)

## Context

Action 1 removes the EchoTribe-internal surface in `PG_LAUNCH_RUNBOOK.md` "Deferred work": `/archer/ads*`, `/archer/organic`, `/archer/products` (page), `/archer/generate_link`, `/archer/generate_ad_copy`, `/archer/campaigns/*`, `/archer/discovery/top_clicked`, `/urlgenius/*` admin, `/levanta/*`, `/webhooks/levanta`; the ad/campaign/organic/urlgenius templates; `campaign_builder.py` and `link_builder.py` if `git grep` clean; and `LevantaAPI`/`LevantaNetworkMatcher`/`ArcherNetworkMatcher` if clean.

P0.6 covers what Action 1 leaves behind: orphaned callers, duplicated helpers, a dead archive directory, prompt builders whose only callers Action 1 removes. The 2026-05-17 realignment means deletions in shared framework files (`prompts.py`, `utils/`, `collection_service.py`) happen on Echo-Dashboard first and sync down; only Shop-MomandMe-local artifacts can be deleted client-side.

## Approach

### S1. `archive/` directory — delete

- Delete `archive/` (entire tree: `archive/routes/archived_routes.py`, plus four HTML files in `archive/templates/`).
- Safety: `git grep -n "from archive\|import archive\|archive/routes\|archive/templates"` returns zero. `archived_routes.py` is comments-only — no Flask registration, no template loader covers it.
- Lands: **shared (upstream)** — Echo-Dashboard first.
- Commit: `chore(slim): remove archive/ — zero importers, archived_routes is comments-only`

### S2. `tests/test_organic_operations.py` — delete (client-only)

- Delete `tests/test_organic_operations.py`.
- Tests enumerate features Action 1 strips: `test_organic_nav_and_manage_routes_are_available`, `test_post_id_on_organic_redirects_to_dedicated_edit_page`, `test_smart_link_route_returns_urlgenius_id_and_patch_stores_it`, `test_generate_posts_auto_creates_and_stores_urlgenius_link`. After Action 1, these fail at route-lookup or import.
- Safety: `git grep -n "test_organic_operations\|OrganicOperationsTestCase"` returns only the file itself.
- Lands: **client-only**. Echo-Dashboard kept the routes per the 0.6 reversal and keeps the tests.
- Commit: `chore(slim): remove tests/test_organic_operations.py — covers stripped features`

### S3. Orphaned prompt builders — delete after Action 1

Action 1 removes `/archer/generate_ad_copy` (`app.py:3495`, caller of `build_ad_copy_prompt` at `app.py:3511`), `/archer/generate_organic_posts` (route at `app.py:715`, caller of `build_organic_posts_prompt` at `app.py:774`), and `/archer/generate_campaign_package` (route at `app.py:1017`, caller of `build_campaign_package_prompt` at `app.py:1075`). After Action 1 lands downstream, these three builders plus their `_legacy` entries (`prompts.py:300-303`: `STEPH_AD_COPY_PROMPT`, `STEPH_ORGANIC_POSTS_PROMPT`, `STEPH_CAMPAIGN_PACKAGE_PROMPT`) are orphaned.

- Delete from `prompts.py`: `build_ad_copy_prompt` (prompts.py:248), `build_organic_posts_prompt` (prompts.py:252), `build_campaign_package_prompt` (prompts.py:256), and the three matching `_legacy` rows.
- Safety: `git grep -n "build_ad_copy_prompt\|build_organic_posts_prompt\|build_campaign_package_prompt\|STEPH_AD_COPY_PROMPT\|STEPH_ORGANIC_POSTS_PROMPT\|STEPH_CAMPAIGN_PACKAGE_PROMPT"` must return only `prompts.py` itself.
- KEEP: `build_chat_prompt`, `build_chat_products`, `build_caption_prompt`, `build_layer_copy_prompt` — live callers at `app.py:167,168,676,3132`.
- Lands: **shared (upstream)** Echo-Dashboard first. Echo-Dashboard kept the routes, so this blocks on confirmation that the ad-copy / organic / campaign-package flows are no longer used there (Open Question #2).
- Commit: `chore(slim): remove prompt builders orphaned by Action 1`

### S4. Duplicate `is_walmart_product` — consolidate

`collection_service.py:41-47` defines a second `is_walmart_product` with one caller (`collection_service.py:168`). `walmart_storefront_enrichment.py:52-58` is the canonical version (callers at `walmart_storefront_enrichment.py:158,199`). Two implementations with subtly different semantics is exactly the kind of trap to clear before it bites.

- Replace `collection_service.py:168` with `walmart_storefront_enrichment.is_walmart_product(product)` and delete the local def.
- Safety: `git grep -n "is_walmart_product"` shows only those two Python files plus a template-local variable in `templates/shop_landing.html` (Jinja, unaffected).
- Risk: `walmart_storefront_enrichment` checks against `WALMART_NETWORK_VALUES` (a set); `collection_service` checks `== "walmart"` strict. If `WALMART_NETWORK_VALUES` is broader than `{"walmart"}`, the swap changes save-side validation semantics. Audit before commit (Open Question #3).
- Lands: **shared (upstream)** Echo-Dashboard first.
- Commit: `refactor(slim): collapse duplicate is_walmart_product into walmart_storefront_enrichment`

### S5. Investigated but not recommending deletion

- `collection_service.py` vs `collection_content.py` — distinct layers, not refactor residue. `collection_service` owns the published-page table (`collages`); `collection_content` owns drafts and AI generation (`collection_content_drafts`). Both have heavy live callers. Merging requires a new abstraction — out of scope.
- `posts.py` — eight callers under `/archer/posts*` (`app.py:2744-2851`); `/archer/posts/manage` is linked from `hub.html:185` and `partials/admin_header.html:15`, and not on Action 1's removal list. Treat as KEEP unless Open Question #1 resolves otherwise.
- `product_lookup_service.py` — caller at `app.py:654` lives inside `/archer/product/<asin>` (reachable from `/archer/collage`, KEEP). Stays.
- `storefront_chat.py`, `walmart_storefront_enrichment.py` — public storefront uses both; covered by tests.
- `_collection_retailer` (`collection_content.py:30`) vs `utils/retailer_labels.collection_retailer` — same name, different signatures. Three internal callers only. Consolidating needs an adapter; leave it.

## Files affected

- `archive/` (entire tree) — **shared (upstream)**
- `prompts.py` — **shared (upstream)**, blocks on Open Q #2
- `collection_service.py` — **shared (upstream)**, blocks on Open Q #3
- `tests/test_organic_operations.py` — **client-only**

## Verification

After each per-feature commit, on the relevant repo (Echo-Dashboard for shared, Shop-MomandMe for client-only):

1. `python3 -m unittest discover -s tests` passes (Echo-Dashboard baseline 301; Shop-MomandMe baseline = 301 minus Action 1 deletions).
2. `python3 -c "import app"` succeeds — no `ImportError` on boot.
3. `git grep -n <deleted_symbol>` returns expected zero.
4. Smoke: `curl /healthz` → 200; `curl /admin/login` renders; demo storefront on `shop.echotribe.ai` unchanged.

## Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Is `/archer/posts/manage` (app.py:2824) a KEEP feature (linked from `hub.html:185`, `partials/admin_header.html:15`) or an Action 1 oversight? Decides whether `posts.py` lives or dies on Shop-MomandMe. | Action 1 author |
| 2 | Are the ad-copy / organic / campaign-package flows still used on Echo-Dashboard, or kept as "just in case"? Blocks S3 landing upstream. | Kelly + Backend Architect |
| 3 | Does `WALMART_NETWORK_VALUES` resolve to exactly `{"walmart"}`? If broader, S4 changes semantics in `collection_service.save_collage`. | Software Architect (5-min audit pre-commit) |
| 4 | `tests/test_pg_compat_and_defaults.py:412-413` asserts `"build_ad_copy_prompt()"` appears in `app.py`. That test needs updating in lockstep with S3. Confirm test scope. | Software Architect |
| 5 | Is `/archer/product/<asin>` (app.py:651, reachable from KEEP `/archer/collage`) staying as-is in P0.1's `/archer/*` → `/admin/*`/`/api/*` rename? If so, `product_lookup_service.py` stays. | P0.1 lead |
