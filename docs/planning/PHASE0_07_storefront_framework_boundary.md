# Phase 0.7 — Storefront Framework Boundary

> **Erratum (2026-05-17, post-merge):** This plan's **Schema additions** section
> lists six columns (`logo_url`, `primary_color`, `accent_color`, `shop_domain`,
> `meta_title_template`, `meta_description_template`). The implementation that
> actually shipped — PR #42 *"P0.7: per-creator brand override columns on
> creators table"* — adds **eight** columns and uses a Material-Design 4-var
> color contract instead of the plan's `primary_color`/`accent_color` pair:
>
> | Plan name | PR #42 implementation |
> |---|---|
> | `logo_url` | `logo_url` ✓ |
> | `shop_domain` | `shop_domain` ✓ |
> | `meta_title_template` | `meta_title_template` ✓ |
> | `meta_description_template` | `meta_description_template` ✓ |
> | `primary_color` *(plan)* | `brand_primary` + `brand_on_primary` |
> | `accent_color` *(plan)* | `brand_primary_container` + `brand_on_primary_container` |
>
> The 4-var color contract aligns with the CSS custom properties in
> `templates/partials/_brand_vars.html` (PR #41) and the
> `design/_design-system/build-conventions.md` brand-swap contract. When the
> implementation phases below reach the "active-creator resolution" and
> "brand context dict" work, use PR #42's column names, not this plan's.
> A potential follow-up extends to 6 vars by adding `--brand-surface` /
> `--brand-on-surface` for canvas swap — tracked as K1 in
> `OPEN_QUESTIONS_TRACKER.md`.

> **Erratum (2026-05-18, K1 follow-up):** K1 has shipped. The brand-swap
> contract is now **6 variables**, not 4, and `creators` now carries **10**
> brand/metadata columns total (was 8). Added:
>
> | Column | CSS var | Legacy bridge target |
> |---|---|---|
> | `brand_surface` | `--brand-surface` | `--bg` (canvas across all 4 live storefront templates) |
> | `brand_on_surface` | `--brand-on-surface` | `--text` only (see note) |
>
> **Bridge note:** `brand_on_surface` mirrors onto `--text` only, not `--ink`.
> `--ink` is dual-use in `walmart_trending_now.html` (`background:` and
> `border-color:` on `.workbook-run-btn`, `.retailer-tab[aria-pressed="true"]`,
> and `.admin-action`); mirroring `brand_on_surface` there would re-tint the
> button fills. Templates that need brand-aware body text on those screens
> should consume `var(--brand-on-surface)` directly. The narrowed scope is
> documented inline in `templates/partials/_brand_vars.html`.
>
> Both new columns are nullable and remain `NULL` on the demo creator. The
> existing precedence chain (`overrides.json` → active row → demo row →
> framework defaults) unchanged.

## Context

The 2026-05-17 realignment reclassified the storefront (`templates/shop_*`, `templates/walmart_*`, `hub.html`, `admin_login.html`, `partials/`, routes under `/shop/`, `/collections/<slug>`, `/admin/login`, `/admin/hub`, `/healthz`, `/api/*`) from **client-only** to **shared upstream framework**. P0.7 makes it real: adds per-creator branding columns, plumbs an active-creator resolver into render, sweeps hardcoded brand strings from templates, and defines a per-deploy `branding/` override. Unblocks Action 1 (Shop-MomandMe stops editing templates locally — overrides land via row + `branding/`) and seeds Phase 5. No multi-creator-per-deploy routing; each deploy still serves one creator.

## Approach

### Schema additions

In `db_schema.init_schema()` after the existing `creators` block, six idempotent `_add_column_if_missing` calls (PG-native, no migration script):

```sql
ALTER TABLE creators ADD COLUMN logo_url                  TEXT;
ALTER TABLE creators ADD COLUMN primary_color             TEXT;
ALTER TABLE creators ADD COLUMN accent_color              TEXT;
ALTER TABLE creators ADD COLUMN shop_domain               TEXT;
ALTER TABLE creators ADD COLUMN meta_title_template       TEXT;
ALTER TABLE creators ADD COLUMN meta_description_template TEXT;
```

All `NULL`-able, no `DEFAULT`. NULL = "fall back to demo creator, then framework default." Colors `#RRGGBB`. `shop_domain` is the canonical host (e.g. `shop.mommyandmecollective.com`) for the resolver's Phase-5 branch. `*_template` are Python format strings with `{slug}`, `{brand}`, `{title}` placeholders.

### Active-creator resolution

New helper in `app.py` near `SHOP_SUBDOMAIN` (~line 60):

```python
def _resolve_active_creator_id() -> str:
    env_id = (os.environ.get('ACTIVE_CREATOR_ID') or '').strip()
    if env_id:
        return env_id
    host = (request.host or '').split(':')[0].lower() if request else ''
    if host:
        row = db_schema._connect().execute(
            "SELECT id FROM creators WHERE shop_domain = ?", (host,)
        ).fetchone()
        if row:
            return row[0]
    return 'everydaywithsteph'
```

Called from a `before_request` hook ordered **after** `_route_shop_subdomain` (rewrite owns routing; this hook only stamps `g.active_creator_id`). Helpers `current_creator_id()` / `current_creator_row()` read from `g`. Both deploys set `ACTIVE_CREATOR_ID` in Replit Secrets. Host lookup stays dormant until Phase 5. This is the `creator_id` value P0.4 writes to `click_log` and P0.3 stamps on `users`; column type `TEXT` matches existing defaults.

### `brand` context dict

`build_brand_context(creator_id)` returns keys: `creator_id, display_name, handle, brand_label, shop_name, logo_url, favicon_url, primary_color, accent_color, theme_default, shop_domain, meta_title_template, meta_description_template, voice_prompt`. Injected via Flask `context_processor` so every `render_template` sees `brand` without per-view plumbing. Built once per request, cached on `g`. P0.2's `/insights` rebuild renders against this same dict.

### Template sweep

Replace hardcoded strings with `{{ brand.* }}` in framework templates only (`archer_*`, `dashboard.html`, `admin_creators.html`, `organic_*` stay — EchoTribe-internal or stripped by Action 1):

- `templates/walmart_trending_now.html` — lines 6, 7, 10, 11, 17, 18, 127–129 (title, meta, eyebrow, h1, subhead); 200, 345, 352 (literal `creator_id=everydaywithsteph`).
- `templates/walmart_collection_create_post.html` — lines 199, 214, 216, 444, 491–495, 509, 627 (Steph-named copy + `creator_id` fallbacks).
- `templates/shop_landing.html`, `shop_directory.html`, `shop_posts.html` — every literal flagged by `git grep -n "everydaywithsteph\|Mommy & Me\|@EverydaywithSteph" templates/shop_*.html`.
- `templates/partials/` — any header/footer brand label.

Pattern: `{{ brand.shop_name }}`, `{{ brand.logo_url or url_for('static', filename='default_logo.svg') }}`, `<meta property="og:title" content="{{ brand.meta_title_template.format(slug=slug, brand=brand.shop_name) if brand.meta_title_template else brand.shop_name }}">`. Theme picker: `'mommyme'` literal → `{{ brand.theme_default }}`.

### `branding/` directory mechanism

Per-deploy directory at repo root: `logo.png` (or `.svg`/`.webp`, first match wins), `favicon.ico`, optional `overrides.json` (e.g. `{"primary_color": "#abc", "shop_name": "..."}`). Read at framework boot via `_load_branding_overrides()` (new in `app.py`), cached in-process. Precedence in `build_brand_context`: **branding/overrides.json** > **creators row** > **demo creator row** > **framework default**. Assets served via `/branding/<path>` route. Missing or partial `branding/` falls back cleanly — deleting the directory must never break a deploy.

### Demo-creator setup on Echo-Dashboard

**Decision: reuse the existing `everydaywithsteph` row.** Rationale: (1) it already exists in production; all `creator_id` defaults across `collages`, `posts`, `collection_content_drafts`, `campaigns_v3`, `storefront_chat_sessions` point to it — a second row forces a backfill P0.7 avoids; (2) Steph is the demo creator in practice; (3) Shop-MomandMe also defaults to `everydaywithsteph`, so both deploys share `ACTIVE_CREATOR_ID` and diverge only via `branding/`. Action: extend `seed_default_creator()` to populate the six new columns with neutral Echo-Dashboard demo defaults (grey palette, `shop.echotribe.ai`, generic meta templates).

### Sync semantics

Shop-MomandMe inherits framework via cherry-pick (`templates/`, `app.py`, `db_schema.py`). Its overrides — `branding/logo.png`, `favicon.ico`, `overrides.json` with Mommy & Me colors, plus an admin-page update setting `logo_url` / `primary_color` / `shop_domain=shop.mommyandmecollective.com` on its `everydaywithsteph` row — are client-only and never overwrite upstream. Upstream template tweaks render Mommy & Me's branding without downstream code edits. Conflict on a shared template: take upstream per `UPSTREAM_SYNC.md`.

## Files affected

- `db_schema.py` — **shared (upstream)**: six `_add_column_if_missing` on `creators`; `seed_default_creator()` populates new columns.
- `app.py` — **shared (upstream)**: `_resolve_active_creator_id`, `build_brand_context`, `_load_branding_overrides`, `before_request` hook, `context_processor`, `/branding/<path>` route.
- `templates/walmart_trending_now.html`, `walmart_collection_create_post.html`, `shop_landing.html`, `shop_directory.html`, `shop_posts.html`, `partials/*` — **shared (upstream)**: brand-string sweep.
- `tests/test_storefront_framework_boundary.py` (new) — **shared (upstream)**.
- `branding/` (per deploy) — **client-only**.

## Verification

1. **Schema migration** — boot against existing PG; `\d creators` shows six new columns; existing rows unaffected (NULL).
2. **Env-driven switch** — `ACTIVE_CREATOR_ID=everydaywithsteph` renders Steph branding across `/shop/`, `/collections/<slug>`, `/admin/login`; swapping to a second creator row renders the other brand with no template edits.
3. **`branding/` fallback** — delete `branding/`; framework renders against creator-row values; missing `logo_url` falls back to a framework static asset. No 500s.
4. **branding/ override** — drop `branding/logo.png` + `overrides.json` with `{"primary_color": "#ff0000"}`; reload — logo and color change with no DB write.
5. **Test isolation** — new `tests/test_storefront_framework_boundary.py`: (a) resolver env precedence + default; (b) `build_brand_context` precedence; (c) `walmart_trending_now.html` renders against two creator rows with distinct titles/meta; (d) missing `branding/` doesn't raise.
6. **Sync compat** — cherry-pick into Shop-MomandMe + its `branding/`; Mommy & Me branding renders downstream, demo branding upstream, same template code on both.

## Open questions

1. **Color format** — `#RRGGBB` only, or named CSS / HSL? Recommend hex-only for v1.
2. **`overrides.json` validation** — strict schema at load, or accept-anything? Recommend accept-anything to match the framework's tolerant-render posture.
3. **Demo defaults for Echo-Dashboard** — what `primary_color` / `meta_title_template` ships with the demo? Needs UX/Steph input so it doesn't read as Mommy & Me.
4. **`shop_domain` uniqueness constraint** — defer to Phase 5; P0.7's resolver prefers the env var.
5. **Static-asset cache busting** — when `branding/logo.png` changes, how do browsers refresh? Recommend content-hash query string at render time.
6. **Admin UI for new columns** — `admin_creators.html` doesn't expose the six fields. Out of scope for P0.7 (set via SQL or seed); flag before Phase 5.
