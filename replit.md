# Mommy & Me Collective Flask App

## Project Overview
Flask web app for Steph's affiliate marketing business serving:
- Interactive prototype app as home page
- Three linked strategy/architecture documentation pages (Build Plan, Architecture, Connections)
- Live Claude AI-powered chat with affiliate product recommendations
- Mobile-responsive product cards with affiliate links
- Real-time Walmart product search via Walmart Affiliate API v2
- Archer Affiliates integration with product catalog (113,835 ASINs), earnings matching, and link generation
- Levanta integration with live API (9,500+ accessible products), brand resolution, and commission tracking
- URLGenius deep linking for Amazon mobile attribution

## Architecture

### Frontend
- `index.html` - Interactive prototype (HTML/CSS/JS)
- `templates/archer_products.html` - Archer Products page with matched grid, Archer/Levanta catalogs, network toggles, stats bar
- `steph-ai-plan.html` - Build Plan documentation
- `steph-architecture.html` - Architecture documentation
- `steph-connection-map.html` - Connections documentation
- Sticky tab navigation (always visible) for easy page switching

### Backend
- `app.py` - Flask app with `/api/chat`, `/archer/*`, `/levanta/*`, `/urlgenius/*` endpoints
- `product_api.py` - ArcherAPI, LevantaAPI, URLGeniusAPI, NetworkMatcher system, scan engine

### Data Files
- `data/Archer Full Catalog 2026.csv` - Full Archer catalog (113,835 ASINs)
- `data/archer_catalog.db` - SQLite cache of Archer products (for image lookups)
- `data/earnings_latest.csv` - Steph's Amazon earnings CSV (586 ASINs)
- `data/matched_asins.json` - Cross-referenced earnings × network matches with brand expansion
- `data/scan_meta.json` - Scan metadata (timestamps, counts)
- `data/network_cache_levanta.json` - Cached Levanta catalog with brand names, images, commissions

### ASIN Match Scan System
- `asin_match_scan()` cross-references 586 earnings ASINs against Archer (CSV) and Levanta (API)
- Two-pass matching: (1) Direct ASIN match, (2) Brand expansion for matched brands
- Produces `matched_asins.json` with `archer_matched`, `levanta_matched`, `archer_brand_match`, `levanta_brand_match` flags
- Brands matched: Jay Franco, SpaceAid, Wise Owl Outfitters → ~2,060 brand-expanded products

### Levanta Integration
- `LevantaAPI` fetches products via `/api/creator/v1/products` (cursor-paginated)
- Brand names resolved via `/brands` endpoint (`brandId` → `brandName` mapping, cursor-paginated)
- Images from product `image` field (Amazon CDN URLs)
- Cache stores: commission, commission_pct, title, brand, imageUrl, category, price, rating, ratingsTotal
- Catalog browse/search reads from cache for fast local filtering; falls back to live API if cache missing

### Product System
**Hot Score Catalog** (13 pre-vetted products):
- Products 0-9: Original hot products (toys, beauty, baby, home)
- Products 10-12: Kitchen gadgets (OXO utensils, Instant Pot, ChefJet chopper)

**Resolution Logic**:
1. Parse Claude response for `PRODUCTS:` (catalog IDs) or `SEARCH:` (API query)
2. For PRODUCTS: Return catalog items directly
3. For SEARCH:
   - Search hot catalog first (fast, always available)
   - If <3 matches, call Walmart Affiliate API with RSA-SHA256 authentication
   - Combine real-time API results with catalog results
   - Auto-detect category (toys/beauty/baby/home/electronics) for routing

### AI Chat
- **Model**: Claude Opus 4.1 (claude-opus-4-1-20250805)
- **Prompt**: Steph persona with product database and recommendation rules
- **Response Format**: Natural text + `PRODUCTS:` or `SEARCH:` directive at end
- **Product Recommendations**: Returns up to 3 matched products with affiliate links

## API Endpoints

### Archer/Levanta Endpoints
- `GET /archer/products` - Archer Products page
- `GET /archer/matched` - Paginated matched ASINs from matched_asins.json
- `GET /archer/search` - Search/browse Archer and/or Levanta catalogs (network=archer|levanta|both)
- `POST /archer/upload_earnings` - Upload earnings CSV
- `GET /archer/asin_match_scan` - Trigger ASIN match scan
- `GET /archer/scan_status` - Check scan status and catalog sizes
- `POST /urlgenius/smart_link` - Generate URLGenius deep link

### `POST /api/chat`
Request:
```json
{ "message": "user question here" }
```

## Configuration

### Environment Variables (Replit Secrets)
**Required for Claude AI**:
- `ANTHROPIC_API_KEY` - Claude API key

**Required for Archer Affiliates**:
- `ARCHER_USERNAME` - Archer API username
- `ARCHER_PASSWORD` - Archer API password

**Required for Levanta**:
- `LEVANTA_API_KEY` - Levanta Creator API key

**Required for URLGenius**:
- `URLGENIUS_API_KEY` - URLGenius deep link API key

**Required for Walmart Affiliate API v2**:
- `WALMART_API_PUBLIC_KEY` - Walmart Consumer ID (UUID format)
- `WALMART_API_PRIVATE_KEY` - Walmart Private Key (PEM format with `\n` escape sequences)
- `WALMART_PUBLISHER_ID` - Your publisher/affiliate ID

### Deployment
- Command: `python3 -m gunicorn --bind=0.0.0.0:5000 --reuse-port app:app`
- Autoscale enabled
- Dependencies: gunicorn, requests, beautifulsoup4, lxml, cryptography, python-dotenv

### `shop.echotribe.ai` Public Landing-Page Subdomain
Published collections (built via the Content Builder Mode C → "Save & Publish" or
the Collage editor) are reachable at:

```
https://shop.echotribe.ai/<slug>
```

How it works:
1. DNS for `shop.echotribe.ai` should point at the same Replit deployment serving
   the dashboard (CNAME or A record).
2. `app.py` registers a `before_request` hook (`_route_shop_subdomain`) that
   detects the host header. When the host matches `SHOP_SUBDOMAIN`, requests of
   the form `GET /<slug>` are rewritten to the existing `shop_landing(slug)`
   handler — so the public surface area is just landing pages and the
   `/archer/track_click` POST endpoint that the rendered page calls back to.
3. Override the host with the `SHOP_SUBDOMAIN` env var if you point a different
   domain at the app (e.g. for staging).

The dashboard host (e.g. `dash.echotribe.ai` or the raw Replit URL) continues to
serve the full builder UI; only the shop subdomain serves landing pages.

### Collection-as-CTA in the Ad Builder
On `/archer/ads` Step 1, when "Landing page" routing is selected, a collection
picker appears. Choosing a saved collection causes the campaign-package backend
to build all 5 layers' destination URLs as
`https://shop.echotribe.ai/<slug>?utm_*` with `utm_content=l[N]_collection`
instead of per-product URLGenius/Amazon links. The Claude prompt also receives
a context line so headlines reference the bundle rather than a single product.

### Multi-creator + Insights (Phase 2A)

#### Architecture
- `db_schema.py` — runs idempotent migrations on every app boot. Creates
  `creators`, `earnings_amazon`, `attribution_paid` tables and adds
  `creator_id`, `status`, `campaign_types`, `hero_title`, `hero_subtitle`
  columns to `collages`. Seeds Steph as the default creator on first boot.
- `prompts.py` — refactored to be creator-aware. Each builder
  (`build_chat_prompt`, `build_caption_prompt`, etc.) accepts an optional
  `creator_id`. Legacy `STEPH_*` constants remain importable as PEP-562 lazy
  module attributes that resolve to the default creator's pre-rendered
  templates — every existing call site keeps working unchanged.
- `link_builder.py` — `LinkBuilder` protocol + registry. `ArcherURLGenius`
  is the production backend (Amazon via URLGenius wrap of Archer attribution
  links). `ImpactStub` is a placeholder that raises until Phase 2C wires up
  Walmart via Impact API. Existing `_make_smart_link()` in app.py delegates
  here so nothing breaks.
- `insights.py` — joins `click_log × collages × earnings_amazon × attribution_paid`
  with click-weighted Amazon revenue reconciliation by slug. Time-window
  resolver supports today / yesterday / 7d / 30d / custom.

#### Routes added
- `GET /admin/creators` — list + create + edit form (no auth in v1)
- `POST /admin/creators` — upsert a creator
- `GET /admin/creators/<id>` — load a single creator
- `GET /insights?window=…&tab=…&creator_id=…` — analytics dashboard with
  Collections / Posts / Ads tabs
- `GET /shop/` — public directory of all published collections (also served
  at the root of `shop.echotribe.ai`)
- `GET /sitemap.xml` — auto-generated sitemap of all published collections
- `GET /robots.txt` — public robots.txt with /admin /api disallowed

#### CSV upload persistence
`POST /dashboard/upload_csv` now persists rows into `earnings_amazon` keyed by
ASIN + period. Optional form fields `creator_id`, `period_start`, `period_end`
override defaults (current creator + today). Existing top-10 product response
is preserved so the dashboard UI is unchanged.

#### Auto-tagging collections
- Mode C save (`POST /archer/collage/save`) → tags collection as `organic`
  (preserving any existing `paid` tag from prior Ad Builder use)
- Ad Builder use (`POST /archer/generate_campaign_package` with
  `collection_slug`) → tags collection as `paid`
- `GET /archer/collages` returns `campaign_types` so UI can show badges

#### SEO / OG metadata
`templates/shop_landing.html` now emits OG tags, Twitter cards, and
Schema.org `CollectionPage` + per-product `Product` markup. Hero title +
subtitle (new collage columns) drive the title and meta description; first
product image becomes `og:image`. The shop subdomain root serves
`templates/shop_directory.html` — a cross-creator listing of all published
collections with creator badges.

#### Draft / Preview
Collages with `status != 'published'` 404 publicly. Append `?preview=1` to
the slug URL to render the page with a yellow "DRAFT PREVIEW" banner.

## Files
- `app.py` - Flask server with all endpoints
- `product_api.py` - ArcherAPI, LevantaAPI, URLGeniusAPI, NetworkMatcher classes
- `templates/archer_products.html` - Archer Products page template
- `index.html` - Frontend with product cards UI
- `steph-ai-plan.html`, `steph-architecture.html`, `steph-connection-map.html` - Documentation pages
- `pyproject.toml` - Dependencies
