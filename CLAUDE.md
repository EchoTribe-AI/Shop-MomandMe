# Shop-MomandMe — Conventions for Claude Code sessions

This file auto-loads in every Claude Code session in this repo. Read it before starting work.

## Architecture (locked)

- This is the **downstream client deploy**. Echo-Dashboard is the **upstream** master app.
- This deploy serves **shop.mommyandmecollective.com**.
- Shared framework code lands in Echo-Dashboard first, then syncs here via cherry-pick per `docs/UPSTREAM_SYNC.md`.
- Baseline tag `v1.0-pg-launch` exists on both repos.
- `branding/overrides.json` carries Steph's Sage Forward palette — these values override upstream defaults for all brand-context lookups.
- `SHOP_SUBDOMAIN` in this deploy's Replit Secrets should be set to `shop.mommyandmecollective.com`.

## Current project state — decisions Claude Code sessions need on first read

### Variant picks (locked 2026-05-18)

| Variant | Surface | Pick | Status |
|---|---|---|---|
| V1 | hub_home | **B** (urgency-first dashboard) | Build pending — needs inline "Manage your collections" link |
| V2 | custom_collections_builder | **B** (filters in sheet) | Deferred build |
| V3 | client_insights_dashboard | **A** (scrolling sections) | Built via P0.2 PR #88/#89 (Echo-Dashboard) + PR #13 (Shop-MomandMe). Gated behind `INSIGHTS_V2_ENABLED`. |
| V4 | product_discovery_feed | **B** (2-col grid + FAB) | Deferred build. Add destination = EchoAgent suggests new-or-existing collection (K22 → R22). |
| V5 | ai_generator_review_queue | **A** (full preview rows) | Deferred until AI generator features in scope. |
| V6 | echoagent_chat | **B** (nav collapses on focus) | Deferred until P2.5 scope. |

### Steph's confirmed brand palette — "Sage Forward" (R14)

| Column | Hex | Role |
|---|---|---|
| `brand_primary` | `#7C7D6A` Sage | Primary CTAs |
| `brand_on_primary` | `#F5F2ED` Off-white | Text on sage |
| `brand_primary_container` | `#DDBBA4` Blush rose | Secondary surfaces |
| `brand_on_primary_container` | `#3D3A33` Charcoal | Text on blush rose |
| `brand_surface` | `#E5DBC8` Linen | Canvas |
| `brand_on_surface` | `#3D3A33` Charcoal | Body text on linen |

Values are live in `branding/overrides.json` at the repo root — reading that file confirms current state. The override layer beats any creator-row values, which means changing brand on this deploy is a JSON edit, not a DB write.

### Where things live

- **Variant mockups**: `design/_selected/<surface>/` for winners; `design/_reference/<surface>/` for losers if retired. Use these when implementing a variant pick.
- **Design system + build conventions**: `design/_design-system/build-conventions.md`.
- **Phase planning docs**: `docs/planning/PHASE0_*.md` (P0.1 through P0.7) — synced from Echo-Dashboard.
- **Project-folder trackers (archive)**: `/Users/kellmaster/Documents/Claude/Projects/EchoTribe Dashboard/` — STATE_OF_PLAY.md, PROJECT_TRACKER.md, OPEN_QUESTIONS_TRACKER.md, ARCHITECTURE_ROADMAP.md, ECHOTRIBE_FEATURE_LIST_V3.md. Not in any repo. Live tracking moved to GitHub Projects.
- **Live tracker**: GitHub Projects "EchoTribe Active Tracker" at github.com/orgs/EchoTribe-AI/projects/.

### Open Steph questions

S2 (SVG logo — PNG works for v1), S5, S6, S7, S8. None block her create-from-Trends workflow testing.

## Brand context (P0.7 + K1)

- Use `g.active_creator_id` (resolved by `_resolve_active_creator_id` in `app.py`) for runtime creator selection. NEVER hardcode `'everydaywithsteph'` at request-time call sites. Seed defaults, test fixtures, and framework-fallback constants may keep the literal.
- In templates: use `{{ brand.shop_name }}`, `{{ brand.logo_url }}`, `{{ brand.brand_primary }}` etc. — never bare strings or hex codes for brand-aware values.
- CSS variables: the 6-var contract is canonical — `--brand-primary`, `--brand-on-primary`, `--brand-primary-container`, `--brand-on-primary-container`, `--brand-surface`, `--brand-on-surface`. Defined in `templates/partials/_brand_vars.html` with static Creator Core fallbacks. Legacy `--accent` / `--soft` / `--bg` / `--text` bridge to brand context when active creator has those columns set; `--ink` is dual-use (button background in places) and does NOT bridge.
- Schema: brand columns live on the `creators` table — 10 nullable TEXT columns total (logo_url, shop_domain, meta_title_template, meta_description_template, brand_primary, brand_on_primary, brand_primary_container, brand_on_primary_container, brand_surface, brand_on_surface).
- Override precedence (in `build_brand_context`): `branding/overrides.json` > active creator row > demo creator row > framework defaults.

## Audit policy

- **Skip audit (Claude Code → merge directly when tests green):** single-template UI tweaks, visual polish, copy changes, full single-screen redesigns (even multi-file), cosmetic CSS, test-only additions, clean sync PRs, visual polish on recently-audited files.
- **Keep full audit (Codex + Kelly review before merge):** schema migrations, auth/security/route-guard changes, sync mechanics (cross-repo cherry-pick logic, route rewrites), first-time additions to upstream framework boundary, anything that could cause data loss or expose private state.

When in doubt: default to no audit unless explicit framework/data risk. Speed matters more than ceremony.

## Parallel sessions

If another Claude Code session may be active in this repo, use `git worktree`:

```
git worktree add /path/to/shop-momandme-<feature-name> -b feature/<feature-name>
cd /path/to/shop-momandme-<feature-name>
```

NEVER use `git switch -c` from a shared directory if another session might be working — branches collide.

## Replit-managed environment

- Production runs on Replit with Postgres. `DATABASE_URL` is injected by Replit's PG integration — not in the Secrets panel.
- User-controlled secrets that matter: `SECRET_KEY`, `ADMIN_PASSWORD`, `SHOP_SUBDOMAIN`, feature flags like `ACTIVE_CREATOR_ID`, `INSIGHTS_V2_ENABLED`.
- `SHOP_SUBDOMAIN` must be `shop.mommyandmecollective.com` in this deploy's Replit Secrets.
- The shop-subdomain route rewriter has a passthrough list — `/admin`, `/branding`, `/healthz`, `/static`, `/api`, `/webhooks`, `/hub`. Anything new that should bypass the rewrite must be added there.

## Backend compatibility

- Code runs on Postgres (production) and SQLite (tests). When querying schema metadata, use `db_schema._USE_PG` to branch between `information_schema` (PG) and `sqlite_master` (SQLite).
- `_add_column_if_missing(conn, 'creators', "<col> TEXT")` is the idempotent migration pattern for new columns. Run on every boot via `_ensure_schema_ready()`.

## Tracking

- Live work tracker: GitHub Projects (org-level "EchoTribe Active Tracker" at github.com/orgs/EchoTribe-AI/projects/).
- Archive: `.md` trackers at `/Users/kellmaster/Documents/Claude/Projects/EchoTribe Dashboard/` (not in any repo) — STATE_OF_PLAY, PROJECT_TRACKER, OPEN_QUESTIONS_TRACKER, ARCHITECTURE_ROADMAP, ECHOTRIBE_FEATURE_LIST_V3.

## Common gotchas

- `click_log` has no `creator_id` — scope queries via JOIN to `collages.creator_id` / `posts.creator_id`.
- `_route_shop_subdomain` only fires when `request.host == SHOP_SUBDOMAIN`. On Replit preview URLs the rewrite doesn't fire; use `/shop/` directly to test storefront.
- Logo file at `branding/logo.png` is currently 1024×1024 (1.5MB). Resize is queued cleanup.
- `walmart_collection_create_post.html` is the canonical 3-step collection editor (Content / Design / Preview). Stays as the editor flow — not variant-tested for redesign.

## Don't

- Don't replace `'everydaywithsteph'` literals in seed functions, test fixtures, or resolver fallbacks.
- Don't widen the `_brand_vars.html` legacy bridge to `--ink` (dual-use; would re-tint button backgrounds).
- Don't add new route handlers without checking if they need adding to the subdomain-rewriter passthrough list.
- Don't propose multi-PR sequences when one PR works.
- Don't add audit ceremony to single-surface UI work.
