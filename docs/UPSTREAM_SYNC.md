# Upstream Sync Workflow (Shop-MomandMe ← Echo-Dashboard)

> **Read this before editing any file flagged in §"Shared surface" below.**
> Edits to shared modules go upstream first, then come back here via cherry-pick. This is the only sustainable way to keep two repos that share a backbone aligned.

---

## Relationship

| | |
|---|---|
| **Upstream** | `EchoTribe-AI/Echo-Dashboard` — `main` branch |
| **Downstream (this repo)** | `EchoTribe-AI/Shop-MomandMe` — `main` branch |
| **Direction** | One-way: upstream → downstream. **Never the reverse.** |
| **Baseline** | Tag `baseline-v1.0` (= upstream `v1.0-pg-launch`), set 2026-05-17 |
| **Cadence** | Weekly until launch (`shop.mommyandmecollective.com`), monthly thereafter |
| **Method** | **Cherry-pick** (preferred), not merge — see §"Why cherry-pick" |

The strategic context (why this split exists, what each app owns, agent roster) lives in:
- `~/Documents/Claude/Projects/EchoTribe Dashboard/ARCHITECTURE_ROADMAP.md`

---

## Architecture model: shared framework + per-creator overrides

> **2026-05-17 realignment.** Earlier versions of this doc treated the entire storefront (templates, /shop/, /collections/, /admin/login, partials) as "client-only" so each deploy edited those files locally. That model didn't scale: a single template fix had to be duplicated across every creator app, and improvements diverged instead of converging.
>
> The new model: **Echo-Dashboard hosts the storefront framework against a demo creator on `shop.echotribe.ai`.** Storefront templates, framework routes, and the admin surface are **shared upstream**. Per-creator differences — brand logo, colors, custom domain, SEO defaults, voice prompt, theme — live in the `creators` DB row (with new columns added in P0.7 of the Phase 0 plan) and in a small per-deploy `branding/` directory.
>
> Improvements to the client app get developed and tested on Echo-Dashboard against the demo creator, then sync downstream to Shop-MomandMe like the rest of the shared surface. Shop-MomandMe overrides only what's actually creator-specific. P0.7 (Software Architect agent in the Phase 0 planning docs) defines the schema additions and the branding-override mechanism that make this work.

---

## One-time setup *(already done; documented for repo continuity)*

```bash
git remote add upstream https://github.com/EchoTribe-AI/Echo-Dashboard.git
git fetch upstream --tags
git tag baseline-v1.0 v1.0-pg-launch
```

Verify:
```bash
git remote -v                                # should list both origin and upstream
git tag -l "baseline-*" "v1.0-*"             # should show baseline-v1.0 and v1.0-pg-launch
```

---

## Shared surface (track upstream; do not edit locally)

These files/modules are owned by upstream. Local edits will be lost on next sync. **If you need to change something on this list:** open the PR in `Echo-Dashboard`, merge to its `main`, then cherry-pick that commit here.

### Backbone (data, services, integrations)
- `db_schema.py`
- `walmart_trends.py`
- `amazon_trends.py`
- `product_api.py` *(WalmartAPI, ImpactAPI, URLGeniusAPI, CrawlbaseAPI, AmazonAPI helpers — minus any Levanta/Archer-only classes confirmed unused on Shop-MomandMe)*
- `utils/amazon_creators.py`
- `collection_service.py`
- `collection_content.py`

### Storefront framework (NEW — moved from "client-only" in the 2026-05-17 realignment)
- `templates/shop_landing.html`, `templates/shop_directory.html`, `templates/shop_posts.html`
- `templates/walmart_collection_create_post.html`, `templates/walmart_trending_now.html`
- `templates/hub.html`, `templates/admin_login.html`, `templates/dashboard.html`
- `templates/organic_post_edit.html`, `templates/organic_posts_manage.html` *(if kept post-strip-down)*
- `templates/partials/` (entire directory)
- Routes under `/shop/`, `/collections/<slug>`, `/admin/login`, `/admin/hub`, `/admin/logout`, `/healthz`, `/api/*`
- Future framework routes: `/insights`, `/discovery` — implemented as creator-scoped, not Shop-MomandMe-specific (see P0.2 plan)
- The `_route_shop_subdomain` middleware in `app.py`

### Admin auth & boot
- Admin auth functions: `_require_walmart_trends_admin`, `_require_admin_page`, `_walmart_content_demo_allowed`, `_admin_session_authed`, `_admin_config_missing`, `_is_production_env`, `require_admin_api`, `require_admin_page` decorators
- `_ensure_schema_ready` lazy initializer
- Production fail-closed posture (audit follow-up 0.4): if `DATABASE_URL` is set and `SECRET_KEY`/`ADMIN_PASSWORD` are missing, admin paths return 503 while `/healthz` stays 200
- `.replit` deploy config (gunicorn/timeout/healthcheck shape; per-deploy domain/port can override locally)

### Docs
- `docs/PG_LAUNCH_RUNBOOK.md`
- `docs/walmart_trending_now.md`
- `docs/amazon_trending_now.md`
- `docs/planning/` *(new — Phase 0 sub-phase implementation plans)*

### Tests
- `tests/test_pg_compat_and_defaults.py`
- `tests/test_admin_config_failclosed.py`
- `tests/test_amazon_creators.py`, `tests/test_amazon_phase3.py`, `tests/test_amazon_trends.py`
- `tests/test_walmart_trends.py`, `tests/test_walmart_storefront_cleanup.py`
- `tests/test_collection_publishing.py`
- `tests/test_impact_links.py`
- `tests/test_workbook_import.py`
- `tests/test_storefront_chat.py`
- `tests/test_retailer_labels.py`
- `tests/test_organic_operations.py`

---

## Client-only surface (safe to edit locally)

Everything below is owned by Shop-MomandMe and will never be overwritten by a sync.

### Brand overrides
- `branding/` *(new directory introduced in P0.7)* — Mommy & Me Collective logo, favicon, brand-color overrides, custom-domain config that the framework reads at render time
- Per-creator row(s) in the `creators` DB table — `display_name`, `handle`, `brand_label`, `voice_prompt`, `theme_default`, `logo_url`, `primary_color`, `accent_color`, `shop_domain`, `meta_title_template`, `meta_description_template`, `defaults_json` (column additions defined in P0.7)
- Per-creator secrets in Replit Secrets (Mommy-and-Me-specific Anthropic key, Walmart Impact account ID/token, URLGenius API key, Amazon Creators account info)

### Per-deploy config
- The deployed `.replit` file's per-domain bits (Cloud Run target host, port, deploy hooks) — the *shape* is shared upstream, but the per-deploy values can diverge
- `seed_default_creator()` invocation arguments (which creator row to seed at boot — `everydaywithsteph` on this deploy, the demo creator on Echo-Dashboard)

### Creator-specific tests
- `tests/test_mommyandme_*.py` *(naming convention)* — anything testing Mommy-and-Me-specific data, copy, or behavior

### Anything not framework
- Genuinely Shop-MomandMe-only product copy, marketing pages, or feature branches that aren't part of the creator-platform framework

**Rule of thumb:** if another future client app would also want this feature, **lift it upstream**. The framework should converge, not fragment.

---

## Why cherry-pick (not merge)

Echo-Dashboard is the internal master app. It will accumulate:
- `/archer/*` ad-ops routes (EchoTribe-internal — *not* in this repo)
- Multi-creator admin tooling
- EchoBoost campaign builders
- Levanta / paid-channel integrations
- Brand-side dashboards

None of that belongs in Shop-MomandMe. Merging `upstream/main` would drag all of it in, then we'd spend the sync resolving deletions. Cherry-pick lets us pull only commits that touch the shared surface above (which now includes the storefront framework).

---

## Weekly sync ritual (~30 min until launch)

```bash
# 1. Fetch the latest upstream
git fetch upstream

# 2. See what's new on upstream that touches shared files
git log --oneline HEAD..upstream/main -- \
  db_schema.py walmart_trends.py amazon_trends.py product_api.py \
  utils/ collection_service.py collection_content.py \
  templates/ \
  docs/PG_LAUNCH_RUNBOOK.md docs/walmart_trending_now.md docs/amazon_trending_now.md \
  docs/planning/ \
  tests/test_pg_compat_and_defaults.py \
  tests/test_admin_config_failclosed.py \
  tests/test_amazon_creators.py tests/test_amazon_phase3.py tests/test_amazon_trends.py \
  tests/test_walmart_trends.py tests/test_walmart_storefront_cleanup.py \
  tests/test_collection_publishing.py tests/test_impact_links.py \
  tests/test_workbook_import.py tests/test_storefront_chat.py \
  tests/test_retailer_labels.py tests/test_organic_operations.py

# 3. For each relevant commit, cherry-pick onto a sync branch
git checkout -b sync/upstream-$(date +%Y-%m-%d)
git cherry-pick <sha1> <sha2> ...

# 4. Resolve any conflicts (favor upstream's version on shared files;
#    favor local on client-specific files like branding/ and tests/test_mommyandme_*.py)

# 5. Run tests
python3 -m unittest discover -s tests

# 6. Push the sync branch and open a PR
git push origin sync/upstream-$(date +%Y-%m-%d)
gh pr create --base main --title "Sync: upstream → main ($(date +%Y-%m-%d))" \
  --body "Cherry-picks from EchoDashboard touching the shared surface."

# 7. After merge, advance the baseline tag
git tag -f baseline-latest upstream/main
```

---

## Conflict resolution rules

| Scenario | Resolution |
|---|---|
| Conflict on a file in §"Shared surface" | Take upstream's version. If local needs differ, **fix upstream**, then re-sync. The framework is the canonical source. |
| Conflict on a file in §"Client-only surface" (`branding/`, `tests/test_mommyandme_*.py`, per-deploy `.replit` values) | Take local. Upstream shouldn't be touching these. If it is, raise a question — the boundary may be wrong. |
| Conflict on a file not listed in either section | Pause and decide which side it belongs to. Default to **shared** unless it's genuinely creator-specific. Add it to §"Shared surface" or §"Client-only surface" before proceeding. The list is incomplete; this is normal early on. |

---

## Post-launch (monthly cadence)

Same ritual, lower frequency. Add to the calendar so the gap doesn't quietly grow to "what did this commit even do?" range.

If a critical fix lands upstream between scheduled syncs (security, data loss, deploy-blocker), cherry-pick that single commit immediately and tag the sync branch `sync/hotfix-<short-desc>` instead of dated.

---

## Anti-patterns (lessons learned)

- **Hardcoding brand strings into templates.** The 2026-05-17 realignment exists because templates were `client-only` and brand text was being edited per-deploy. Render brand from the creator row, not from template constants.
- **Editing a shared file locally to fix something fast.** Even if the diff is small, the next sync will overwrite it. Open the upstream PR, then sync down. The 30-minute delay is worth the boundary discipline.
- **Treating `tests/` as monolithic.** Most test files are shared; `tests/test_mommyandme_*.py` is the only client-only convention. Don't add Mommy-and-Me-specific assertions to shared test files — they'll break on Echo-Dashboard's demo creator data.

---

## Pointer: changes that must always sync immediately

These are non-negotiable — sync within 24h of upstream merge, not at the weekly cadence:

1. Any change to admin auth (`_require_walmart_trends_admin`, `_require_admin_page`, `_walmart_content_demo_allowed`, `_admin_config_missing`, `require_admin_api`/`require_admin_page` decorators) — security
2. Any change to `db_schema.py` that adds/alters columns — schema drift between apps is silent corruption
3. Any change to `/healthz` — Cloud Run health probe
4. Any change to `.replit` deploy config (the shape, not per-deploy values)
5. Any change to the production fail-closed posture (audit follow-up 0.4)
6. Any change to the `_route_shop_subdomain` middleware — host/route rewriting touches every public request

If you see one of these on upstream `main`, treat it like a hotfix.
