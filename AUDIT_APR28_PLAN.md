# April 28 Audit — Shop + Insights UX Follow-up

Date: 2026-04-28
Branch scope: current working branch with AI chat enabled

## Audit Summary by Requested Item

### 1) Add AI chat + FTC disclosure to `/shop` home (directory)
**Status:** Not implemented on directory page.

- `/shop/<slug>` landing already includes AI chat and affiliate disclosure (`templates/shop_landing.html`).
- `/shop/` directory uses `templates/shop_directory.html` and currently renders only collection cards; no chat module or disclosure text.
- Result: requirement is only partially met (present on collection landing, missing on directory home).

### 2) New shop page for social posts (mobile-friendly list/grid, newest first)
**Status:** Missing dedicated public page.

- Existing public routes are `/shop/` and `/shop/<slug>` for collections.
- Social posts exist in DB and are surfaced in insights/admin contexts, but there is no public shop page that lists social posts in the collection-card design system.
- Sorting requirement (most recent default) and mobile 2-up/1-up behavior are not currently implemented for social posts because the page does not exist yet.

### 3) `aprl28-trends` should show full product title (not truncated)
**Status:** Reproducible in current template.

- Product name is rendered in `.pc-name` with CSS truncation (`-webkit-line-clamp: 2; overflow: hidden;`).
- This will cut long titles with implicit ellipsis behavior on WebKit-based browsers.
- Result: requirement conflict is confirmed.

### 4) `aprl28-trends` missing `$` symbol on some product prices
**Status:** Reproducible via display logic.

- Price renders raw as `{{ product.price or '' }}` in the landing card template.
- If saved product JSON has values like `19.99` instead of `$19.99`, the symbol is omitted.
- Result: formatting is data-dependent and inconsistent; no normalization layer on render.

### 5) Insights drill-in/edit for collections, posts, and ads
**Status:** Only partial drill-in exists.

- Collections tab links to `/shop/<slug>` (view-only).
- Posts tab links only to the related collection slug when present; no post editor deep-link.
- Ads tab is read-only metrics table.
- No direct “edit” entry point from Insights for collection product fields (image/link/title/price) or post/ad artifacts.

---

## Recommended Implementation Plan (for approval)

## Phase 1 — Fast UX fixes (low risk, high impact)
1. **Directory chat + disclosure parity**
   - Add reusable chat/disclosure component to `templates/shop_directory.html`.
   - Reuse `/api/shop/chat` endpoint with directory-aware context (`slug='shop-home'`).
   - Keep disclosure copy consistent with landing pages.

2. **Title + price rendering fixes in landing cards**
   - Remove clamp on `.pc-name` for full title display (or make configurable per page).
   - Add server/template helper to normalize display price:
     - Prefix `$` for numeric values.
     - Preserve existing currency symbol if present.
     - Handle ranges safely.

**Acceptance checks**
- `/shop/` shows chat module and FTC disclosure at bottom.
- `/shop/aprl28-trends` product titles no longer truncate.
- All prices on `/shop/aprl28-trends` render with `$` when USD numeric input is provided.

## Phase 2 — New public social-post shop page
3. **Create `/shop/posts` public page**
   - Add route and template in current collection visual style.
   - Load published/saved social posts with thumbnail + title/copy excerpt + CTA.
   - Default sort: newest first (`posted_at`/`created_at` descending).
   - Responsive behavior:
     - mobile compact mode: 1 card/row
     - mobile grid mode: 2 cards/row
     - desktop can stay 2–3 based on existing style system.

4. **Card interaction model**
   - Click-through to associated collection page when available.
   - Optional fallback “View post details” modal/page when no collection slug.

**Acceptance checks**
- `/shop/posts` is reachable from `/shop/` navigation.
- Default ordering is newest→oldest.
- Mobile verified for both 2-up grid and condensed 1-up mode.

## Phase 3 — Insights drill-in and edit workflow
5. **Collections drill-in/edit from Insights**
   - Add actions: `View` + `Edit` in collections tab rows.
   - `Edit` deep-links to existing collage builder preloaded with slug.

6. **Posts drill-in/edit from Insights**
   - Add links to post queue/editor with `post_id` focus.
   - Enable editing core fields (copy, product title, price, image note, link).

7. **Ads drill-in**
   - Add link from ads row to campaign detail (existing ad builder or new lightweight detail view).
   - Include editable metadata where supported (name/label/status/routing fields).

**Acceptance checks**
- From each insights tab, at least one clear “View” and one “Edit” pathway exists.
- Editing updates persist and are reflected in corresponding landing pages.

---

## Suggested Delivery Order
1. Phase 1 (quick wins)
2. Phase 2 (new page)
3. Phase 3 (cross-module drill-in/edit wiring)

This order minimizes regression risk and delivers immediate visible improvements while larger navigation/edit workflows are implemented after UI parity and display correctness are stable.
