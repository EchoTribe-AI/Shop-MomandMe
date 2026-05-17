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

These files/modules are owned by upstream. Local edits will be lost on next sync.

- `db_schema.py`
- `walmart_trends.py`
- `amazon_trends.py`
- `product_api.py` *(WalmartAPI, ImpactAPI, URLGeniusAPI, CrawlbaseAPI, AmazonAPI helpers)*
- `utils/amazon_creators.py`
- `collection_service.py`
- `collection_content.py`
- Admin auth: `_require_walmart_trends_admin`, `_walmart_content_demo_allowed`, `_admin_session_authed`, `/admin/login` route + `templates/admin_login.html`
- `/healthz` route, `_ensure_schema_ready` lazy initializer
- `.replit` deploy config
- `tests/test_pg_compat_and_defaults.py`
- `tests/test_amazon_creators.py`, `tests/test_amazon_phase3.py`, `tests/test_amazon_trends.py`
- `tests/test_walmart_trends.py`, `tests/test_walmart_storefront_cleanup.py`
- `tests/test_collection_publishing.py`
- `tests/test_impact_links.py`
- `docs/PG_LAUNCH_RUNBOOK.md`, `docs/walmart_trending_now.md`, `docs/amazon_trending_now.md`

**If you need to change something on this list:** open the PR in `Echo-Dashboard`, merge to its `main`, then cherry-pick that commit here.

---

## Client-only surface (safe to edit locally)

Everything below is owned by Shop-MomandMe and will never be overwritten by a sync:

- `templates/` storefront pages, hero copy, brand visuals
- `static/` assets (logos, fonts, theme CSS)
- Any new client-facing routes added under `/shop/`, `/insights`, `/discovery` *that don't duplicate shared logic*
- Per-creator config (creator_id row, voice prompt, brand label) — lives in DB, not in code
- Client-specific tests under `tests/test_mommyandme_*.py` (naming convention to keep them obvious)

If you find yourself adding a feature that other future client apps will also want, **lift it upstream** instead of duplicating. Repo discipline >> local convenience.

---

## Why cherry-pick (not merge)

EchoDashboard is the internal master app. It will accumulate:
- `/archer/*` ad-ops routes
- Multi-creator admin tooling
- EchoBoost campaign builders
- Levanta / paid-channel integrations
- Brand-side dashboards

None of that belongs in Shop-MomandMe. Merging `upstream/main` would drag all of it in, then we'd spend the sync resolving deletions. Cherry-pick lets us pull only commits that touch the shared surface above.

---

## Weekly sync ritual (~30 min until launch)

```bash
# 1. Fetch the latest upstream
git fetch upstream

# 2. See what's new on upstream that touches shared files
git log --oneline HEAD..upstream/main -- \
  db_schema.py walmart_trends.py amazon_trends.py product_api.py \
  utils/ collection_service.py collection_content.py \
  docs/PG_LAUNCH_RUNBOOK.md docs/walmart_trending_now.md docs/amazon_trending_now.md

# 3. For each relevant commit, cherry-pick onto a sync branch
git checkout -b sync/upstream-$(date +%Y-%m-%d)
git cherry-pick <sha1> <sha2> ...

# 4. Resolve any conflicts (favor upstream's version on shared files;
#    favor local on client-specific files)

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
| Conflict on a file in §"Shared surface" | Take upstream's version. If local needs differ, **fix upstream**, then re-sync. |
| Conflict on a file in §"Client-only surface" | Take local. Upstream shouldn't be touching these. If it is, raise a question — the boundary may be wrong. |
| Conflict on a file not listed in either section | Pause and decide which side it belongs to. Add it to §"Shared surface" or §"Client-only surface" before proceeding. The list is incomplete; this is normal early on. |

---

## Post-launch (monthly cadence)

Same ritual, lower frequency. Add to the calendar so the gap doesn't quietly grow to "what did this commit even do?" range.

If a critical fix lands upstream between scheduled syncs (security, data loss, deploy-blocker), cherry-pick that single commit immediately and tag the sync branch `sync/hotfix-<short-desc>` instead of dated.

---

## Anti-patterns (lessons learned, will be added as we hit them)

- *(none yet — append as the team encounters them)*

---

## Pointer: changes that must always sync immediately

These are non-negotiable — sync within 24h of upstream merge, not at the weekly cadence:

1. Any change to `_walmart_content_demo_allowed` (admin auth bypass logic — security)
2. Any change to `db_schema.py` that adds/alters columns (schema drift between apps is silent corruption)
3. Any change to `/healthz` (Cloud Run health probe)
4. Any change to `.replit` deploy config

If you see one of these on upstream `main`, treat it like a hotfix.
