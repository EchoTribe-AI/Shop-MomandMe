# Template partials

Shared Jinja partials that ship in **Echo-Dashboard** as the canonical
storefront framework and sync downstream to per-creator deploys
(Shop-MomandMe etc.) per `docs/UPSTREAM_SYNC.md`.

Every partial here is "shared upstream" — edits land in Echo-Dashboard
first, then cherry-pick downstream.

## Inventory

| Partial | Use for | Contract source |
|---|---|---|
| `_brand_vars.html` | Defines the canonical brand-swap CSS variables at `:root`. Include once at the top of any page (auto-included by `_mobile_chrome.html`). | `design/_design-system/build-conventions.md` §Brand styling |
| `_mobile_chrome.html` | Base layout for shop + hub mobile pages — sticky 64px header, 64px bottom nav, 96px content padding. Extend it, override blocks. | build-conventions §Layout |
| `_icon_button.html` | Icon-only buttons (back, overflow, cart-add, favorite, share). **Requires `label` prop** — emits a build-time warning comment if missing. | build-conventions §Accessibility (WCAG 4.1.2) |
| `_status_pill.html` | All status badges (Published / Draft / AI draft / Selling out / Must-have / Price drop / Trending / etc.). Locked Creator Core colors — NOT brand-swappable, by design. | build-conventions §Cards |
| `_section_header_link.html` | "View all" / "See more" / "Edit" links inside section headers. Enforces 44×44 tap target via padding. | build-conventions §Accessibility (WCAG 2.5.5) |
| `_product_card.html` | Single canonical product card. Optional slots for `quote`, `urgency_status`, `essentials_count`. Used by trending, feed, discovery, collection items. | build-conventions §Cards |
| `_guarantee_panel.html` | Iconified bullet rows — high-trust content surface. Ported from B variant of shop landing. | build-conventions §Reusable panels |

## Brand-swap surface

Per-creator deploys override these CSS custom properties (and only these)
in their own per-deploy CSS:

- `--brand-primary`
- `--brand-on-primary`
- `--brand-primary-container`
- `--brand-on-primary-container`

The widening from 4 → 6 variables (adding `--brand-surface` /
`--brand-on-surface` so canvas/ink can be brand-driven) is **K1** in the
project's `OPEN_QUESTIONS_TRACKER.md` — currently pending Kelly decision.

**Locked Creator Core (NOT brand-swappable, by intent):**
- `--brand-success` and container — semantic green for Published / Ready / Must-have
- `--brand-warning` and container — semantic amber for Edited / Trending
- `--brand-danger` and container — semantic red for Selling out / Price drop
- Status pill icon mappings in `_status_pill.html`

Rationale: brand override must not be able to accidentally repaint "ready"
or "selling out" into a creator's brand color and lose the semantic signal.

## How to add a new partial

1. Name with leading underscore: `_my_partial.html`
2. Top-of-file Jinja comment block documenting **Usage**, **Required context vars**, **Optional context vars**
3. Use the canonical brand-swap variables (`var(--brand-primary)` etc.), never hardcode hex
4. Use the `focus-ring` utility class on every focusable element
5. Add tap-target minimums (44×44) on any interactive element
6. Add an entry to this README's Inventory table
7. Reference the build-conventions section that defines the contract

## How to use a partial in a page template

```jinja
{# Base-layout extension #}
{% extends 'partials/_mobile_chrome.html' %}
{% block title %}Trending Picks · {{ creator.brand_label }}{% endblock %}
{% block header_left %}
  {% with icon='arrow_back', label='Back', href='/admin/hub' %}
    {% include 'partials/_icon_button.html' %}
  {% endwith %}
{% endblock %}
{% block content %}
  <h2>Selling out fast</h2>
  {% for product in products %}
    {% include 'partials/_product_card.html' %}
  {% endfor %}
{% endblock %}
```
