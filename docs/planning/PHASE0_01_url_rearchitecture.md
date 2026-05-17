# Phase 0.1 — URL Re-Architecture

## Context

V3 feature list locks the namespace: `/admin/*` (session-gated), `/shop/*` + `/collections/<slug>` (public), `/api/*` (JSON), `/insights`, `/discovery`, `/healthz`. Echo-Dashboard today exposes ~40 `/archer/*` routes mixing admin pages, JSON APIs, and one public click logger. PR #38 (open) normalized auth via `@require_admin_api` / `@require_admin_page` and left `/archer/track_click` intentionally unguarded. Per the 2026-05-17 realignment, framework routes are **shared upstream**; renames happen on Echo-Dashboard `main` and flow downstream via cherry-pick. Shop-MomandMe Action 1 strips the ad-ops `/archer/*` surface — sequencing matters: rename **before** strip-down so the strip-down deletes the renamed routes, not orphan stale paths.

## Approach

**Sequencing.** PR #38 lands first (uniform decorators). Then this rename on Echo-Dashboard, then cherry-pick to Shop-MomandMe, then Action 1 deletes the renamed ad-ops routes downstream.

**Flat rename, not blueprints.** `app.py` is a single Flask app with inline `@app.route` declarations. Blueprints would double the diff and complicate the cherry-pick. Keep `@app.route` and change only the path string; file blueprints as a separate follow-up.

**Route map (old → new).** Function names stay; only the URL changes. Public click logger lands at `/api/clicks` (semantic, not action-y; future-proof for batching).

| Old `/archer/*` | New | Class |
|---|---|---|
| `/archer/products` | `/admin/products` | admin page |
| `/archer/search` | `/api/products/search` | JSON |
| `/archer/product/<asin>` | `/admin/products/<asin>` | admin page |
| `/archer/generate_link` | `/api/links/generate` | JSON |
| `/archer/generate_caption` | `/api/ai/caption` | JSON |
| `/archer/generate_organic_posts` | `/api/ai/posts/organic` | JSON |
| `/archer/generate_posts` | `/api/ai/posts` | JSON |
| `/archer/generate_campaign_package` | `/api/ai/campaigns/package` | JSON |
| `/archer/generate_ad_copy` | `/api/ai/ads/copy` | JSON |
| `/archer/collage` | `/admin/collections/edit` | admin page |
| `/archer/collage/<slug>` | `/api/collections/<slug>` | JSON |
| `/archer/collage/save` | `/api/collections/save` | JSON |
| `/archer/collage/publish` | `/api/collections/publish` | JSON |
| `/archer/collage/archive` | `/api/collections/archive` | JSON |
| `/archer/collage/restore` | `/api/collections/restore` | JSON |
| `/archer/collages` | `/api/collections` | JSON |
| `/archer/posts` (GET, PATCH, DELETE, /bulk, /export.csv) | `/api/posts*` | JSON |
| `/archer/posts/manage` | `/admin/posts/manage` | admin page |
| `/archer/posts/<id>/edit` | `/admin/posts/<id>/edit` | admin page |
| `/archer/campaigns` | `/admin/campaigns` | admin page |
| `/archer/campaigns/list`, `/<id>`, `/generate`, `/boost`, `/<id>/export`, `/fetch-product` | `/api/campaigns/*` | JSON |
| `/archer/ads` | `/admin/ads` | admin page |
| `/archer/organic` | `/admin/posts/organic` | admin page (alias-or-rename TBD; see Open Q3) |
| `/archer/ads/save` | `/api/ads/save` | JSON |
| `/archer/ads/campaigns` | `/admin/ads/campaigns` | admin page |
| `/archer/image_proxy` | `/api/image_proxy` | JSON/binary |
| `/archer/discovery/top_clicked` | `/api/discovery/top_clicked` | JSON |
| `/archer/urlgenius` | `/admin/urlgenius` | admin page |
| `/archer/track_click` | `/api/clicks` | **public JSON** (unguarded) |

**301 redirects.** Add one legacy adapter near the route block: a list of `(old_prefix, new_prefix)` tuples driving `app.add_url_rule` that emits `redirect(new_url, code=301)` preserving query string and trailing slug/id. Unguarded (destination enforces auth). Removal scheduled after one sync cycle past Action 1 (Open Q4).

**Template + JS sweep.** Replace string literals, not just `url_for()`. Only two `url_for()` callsites exist (`app.py:503`, `:3322`); both auto-follow because function names don't change. Sweep targets:

1. `templates/{archer_*,organic_*,urlgenius_links,insights,hub,dashboard,admin_creators,shop_landing}.html` — every `/archer/<x>` literal in `fetch(...)`, `<form action>`, `<a href>`, `window.location`.
2. `templates/partials/{hub_nav,admin_header}.html` — nav links.
3. `static/**/*.js` — `git grep -rn "/archer/" static/`.
4. `shop_landing.html:311` (`fetch('/archer/track_click')`) → `fetch('/api/clicks')`.
5. Host-routing passthrough in `app.py` (~line 123) — update `if path == '/archer/track_click'` to `/api/clicks`.

**Decorator preservation.** Each renamed route keeps its PR #38 guard. `/api/clicks` stays unguarded. Add a regression test asserting no `/admin/*` or `/api/*` route outside the allowlist (`/admin/login`, `/healthz`, `/api/clicks`, `/api/shop/chat`) is reachable without a session.

## Files affected

- `app.py` — shared (upstream)
- `templates/archer_*.html`, `organic_*.html`, `urlgenius_links.html`, `insights.html`, `hub.html`, `dashboard.html`, `admin_creators.html`, `shop_landing.html`, `partials/hub_nav.html`, `partials/admin_header.html` — shared (upstream)
- `static/**/*.js` (those matching `git grep /archer/`) — shared (upstream)
- `tests/test_*.py` — new test file `tests/test_url_rearchitecture.py` — shared (upstream)
- `docs/PG_LAUNCH_RUNBOOK.md` — shared (upstream); update admin entry table and "Test commands" curl examples
- Shop-MomandMe (the downstream repo) — **no direct edits**; receives the rename via the next upstream-sync cycle, then Action 1 deletes the ad-ops `/admin/*` and `/api/*` routes it doesn't own

## Verification

1. Unit/integration tests:
   - `tests/test_url_rearchitecture.py` — for every renamed pair, assert old returns 301 to new (preserving query) and new returns 200 (authed) or 302→login (unauthed).
   - Existing 301-test suite passes: `python3 -m unittest discover -s tests` should grow from 301 to ~330+ tests.
2. End-to-end curl (run from repo root after `python3 main.py`):
   ```
   curl -i http://localhost:5000/healthz                                  # 200
   curl -i http://localhost:5000/archer/posts/manage                      # 301 → /admin/posts/manage
   curl -i -L http://localhost:5000/admin/posts/manage                    # 302 → /admin/login
   curl -i -d "name=dan&password=dan" http://localhost:5000/admin/login -c /tmp/c
   curl -i -b /tmp/c http://localhost:5000/admin/posts/manage             # 200
   curl -i -X POST http://localhost:5000/api/clicks -d '{"asin":"X"}' -H 'Content-Type: application/json'   # 200, no auth
   ```
3. Template sweep proof: `git grep -n "/archer/" -- ':!docs/' ':!CHANGELOG*'` returns zero matches outside the 301 adapter's tuple list.
4. Manual: click through `/admin/hub` → all nav links resolve 200; the storefront click logger fires on `/api/clicks` (verify in Network tab on `shop.echotribe.ai` staging).

## Open questions

1. **`/api/clicks` vs `/api/track_click`?** Plan picks `/api/clicks` (resource-shaped, batch-friendly). Backend Architect to confirm.
2. **`/archer/organic` new name.** Plan picks `/admin/posts/organic`; `/admin/organic` is shorter but loses grouping. UX Architect (P0.2 owner) to resolve since insights flip touches the same nav.
3. **Collage edit URL shape.** Feature list shows `/admin/collections/<slug>/edit`; old code uses `?collection=<slug>` (`app.py:1357`). Path-segment is more REST-y but breaks bookmarks. Software Architect to confirm at PR review.
4. **301 lifespan.** Recommendation: remove after Action 1 lands and one sync cycle passes. Project Shepherd to confirm.
5. **PR ordering vs Action 1.** Rename PR must merge **before** Shop-MomandMe Action 1. Git Workflow Master to confirm.
