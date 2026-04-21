# API_CONNECTIONS.dm
# Steph / Mommy & Me Collective — Full API Connection Manifest
# Generated: 2026-04-21
# Source files: app.py, product_api.py
#
# Purpose: Hand-off reference for building further API calls and running tests.
# Every internal HTTP route and every external API client is documented below
# with: method, path/URL, auth, request shape, response shape, source location,
# example curl, and known gotchas.
#
# Legend:
#   [INT]  = internal Flask route exposed by this app
#   [EXT]  = external third-party API consumed by this app
#   ENV    = environment variable / Replit Secret required
#
# Base URL (local dev):    http://localhost:5000
# Base URL (Replit):       https://$REPLIT_DEV_DOMAIN
# Base URL (deployed):     https://<your-replit-app>.replit.app


# =============================================================================
# 1. ENVIRONMENT VARIABLES (single source of truth)
# =============================================================================

ANTHROPIC_API_KEY        # Claude (chat, captions, ad copy)
ARCHER_USERNAME          # Archer Affiliates OAuth
ARCHER_PASSWORD          # Archer Affiliates OAuth
LEVANTA_API_KEY          # Levanta Creator API bearer
LEVANTA_WEBHOOK_SECRET   # HMAC-SHA256 signing secret for /webhooks/levanta
URLGENIUS_API_KEY        # URLGenius deep links
WALMART_CONSUMER_ID      # Walmart Affiliate API (in WalmartAPI class)
WALMART_PRIVATE_KEY      # Walmart Affiliate API
CRAWLBASE_TOKEN          # Crawlbase Amazon scraper
IMPACT_ACCOUNT_SID       # Impact.com (Walmart conversion links)
IMPACT_AUTH_TOKEN        # Impact.com
AMAZON_AFFILIATE_TAG     # default: mommymedeals-20
FB_PIXEL_ID              # default: 1559451780790812
FLASK_DEBUG              # true/false


# =============================================================================
# 2. EXTERNAL APIS (clients in product_api.py)
# =============================================================================

# -----------------------------------------------------------------------------
# [EXT] Archer Affiliates API
# Class: ArcherAPI (product_api.py:423)
# Base:  https://api.archeraffiliates.com
# Auth:  POST /token with username/password → bearer, cached 55 min
# -----------------------------------------------------------------------------
POST   /token                          # body: username, password (form-encoded)
GET    /getproducts                    # paginated catalog dump
GET    /get_single_product?asin=...    # one product by ASIN
POST   /generate_attribution_link      # body: { asin, label } → tracked URL
GET    /insights                       # account-level performance
GET    /get_affiliateID                # affiliate metadata

# Local cache:  data/archer_catalog.db (SQLite, WAL mode, 30s lock timeout via _db_connect)
# Tables:       products, cache_meta, collages, click_log, campaigns
# Catalog TTL:  24h (CACHE_TTL_HOURS)


# -----------------------------------------------------------------------------
# [EXT] Levanta Creator API
# Class: LevantaAPI (product_api.py:1184)
# Base:  https://app.levanta.io/api/creator/v1
# Auth:  Bearer LEVANTA_API_KEY
# -----------------------------------------------------------------------------
GET    /brands?access=true&marketplace=amazon.com&limit=100&cursor=...
GET    /products?limit=100&cursor=...&marketplace=amazon.com
GET    /products/{asin}?marketplace=amazon.com
POST   /links                          # body: { asin, marketplace, sourceId? }
GET    /deals?limit=50
GET    /cost-per-click-campaigns?limit=50
GET    /reports?limit=100

# Local cache:  data/network_cache_levanta.json
# Pagination:   cursor-based (response.cursor); break when missing


# -----------------------------------------------------------------------------
# [EXT] URLGenius API
# Class: URLGeniusAPI (product_api.py:1391)
# Base:  https://api.urlgeni.us/api/v2
# Auth:  Header  api-key: $URLGENIUS_API_KEY
# -----------------------------------------------------------------------------
POST   /links                          # body: { url, utm:{utm_source,utm_medium,utm_campaign,utm_content,utm_term} }
GET    /links?limit=50
GET    /links/{link_id}                # 30-day stats
DELETE /links/{link_id}                # 204 on success

# Dedup registry: data/urlgenius_registry.json
# Key format:    "{destination_url}||{src}|{med}|{camp}|{content}|{term}"
# Seeded on app startup via _seed_urlgenius()


# -----------------------------------------------------------------------------
# [EXT] Walmart Affiliate API
# Class: WalmartAPI (product_api.py:22)
# Base:  https://developer.api.walmart.com
# Auth:  Signed request (Walmart consumer-id headers)
# -----------------------------------------------------------------------------
GET    /api-proxy/service/affil/product/v2/search?query=...

# -----------------------------------------------------------------------------
# [EXT] Crawlbase (Amazon scraper fallback)
# Class: CrawlbaseAPI (product_api.py:145)
# Base:  https://api.crawlbase.com/
# Auth:  token query param
# -----------------------------------------------------------------------------
GET    /?token=...&url=https://www.amazon.com/s?k={query}    # search
GET    /?token=...&url=https://www.amazon.com/dp/{asin}      # product detail

# -----------------------------------------------------------------------------
# [EXT] Impact.com Mediapartners
# Class: ImpactAPI (product_api.py:209)
# Base:  https://api.impact.com/Mediapartners
# Auth:  Basic (account_sid, auth_token)
# -----------------------------------------------------------------------------
GET    /{account_sid}/Conversions/ConversionLink?...

# -----------------------------------------------------------------------------
# [EXT] Anthropic (Claude)
# Used in:  /api/chat, /archer/generate_caption, /archer/generate_ad_copy
# Models:   claude-sonnet-4-20250514 (chat), claude-sonnet-4-6 (captions/ads)
# Auth:     ANTHROPIC_API_KEY
# -----------------------------------------------------------------------------


# =============================================================================
# 3. INTERNAL ROUTES — CHAT / CORE
# =============================================================================

# -----------------------------------------------------------------------------
# [INT] POST /api/chat                                          app.py:109
# -----------------------------------------------------------------------------
# Body:    { "message": "best toy under $30?" }
# Returns: { "reply": str, "products": [ProductDict, ...] }
# Errors:  400 if message empty; 500 on Claude failure
# Notes:   Claude must end with "PRODUCTS: id,id,id" or "SEARCH: cat terms".
#          Falls back to ProductResolver.resolve() on search indicators.
# Test:
#   curl -sX POST localhost:5000/api/chat -H 'content-type: application/json' \
#        -d '{"message":"best stroller"}'


# =============================================================================
# 4. INTERNAL ROUTES — ARCHER (catalog, scan, products, ads, collages)
# =============================================================================

# -----------------------------------------------------------------------------
# [INT] GET /archer/matched                                     app.py:207
# -----------------------------------------------------------------------------
# Query:   limit (default 20, max 100), offset (default 0)
# Returns: { products:[...], total:int, has_more:bool }
# Source:  data/matched_asins.json
# Test:    curl 'localhost:5000/archer/matched?limit=10&offset=0'

# -----------------------------------------------------------------------------
# [INT] POST /archer/upload_earnings                            app.py:220
# -----------------------------------------------------------------------------
# Body:    multipart form, field name "file", must be .csv
# Side:    saves to data/earnings_latest.csv, immediately runs asin_match_scan()
# Returns: scan result dict + uploaded_filename
# Test:
#   curl -F "file=@earnings.csv" localhost:5000/archer/upload_earnings

# -----------------------------------------------------------------------------
# [INT] GET /archer/asin_match_scan                             app.py:244
# -----------------------------------------------------------------------------
# Re-runs ArcherAPI.asin_match_scan() against last uploaded CSV.
# Writes:  data/matched_asins.json + data/scan_meta.json
# Returns: { scan_id, total_asins, archer_matches, levanta_matches, ... }

# -----------------------------------------------------------------------------
# [INT] GET /archer/force_rescan                                app.py:256
# -----------------------------------------------------------------------------
# Alias of /archer/asin_match_scan (forces rebuild of matched_asins.json).

# -----------------------------------------------------------------------------
# [INT] GET /archer/scan_status                                 app.py:289
# -----------------------------------------------------------------------------
# Returns: {
#   never_run:bool, csv_uploaded:bool, csv_filename:str|null,
#   archer_catalog_size:int, levanta_catalog_size:int,
#   <plus fields from data/scan_meta.json: scan_id, ran_at, totals>
# }

# -----------------------------------------------------------------------------
# [INT] GET /archer/search                                      app.py:332
# -----------------------------------------------------------------------------
# Query:   q, category, min_commission (int %), limit (max 200), offset,
#          network = archer | levanta | both   (default archer)
# Returns: {
#   products:[...combined paged], archer:[...], archer_total:int,
#   archer_catalog_total:113835, levanta:[...], levanta_total:int,
#   levanta_catalog_total:int
# }

# -----------------------------------------------------------------------------
# [INT] GET /archer/levanta_match_scan                          app.py:456
# -----------------------------------------------------------------------------
# One-shot: cross-references data/Amazon_Earnings_2026.csv against
# Levanta accessible products. Writes data/levanta_matches.json.
# Returns: { steph_asins, levanta_asins, matches_found, top_matches[10], saved_to }

# -----------------------------------------------------------------------------
# [INT] GET /archer/backfill_images                             app.py:537
# -----------------------------------------------------------------------------
# One-time: populates image_encoded_string for all matched ASINs.
# Returns: { updated:int, total:int }

# -----------------------------------------------------------------------------
# [INT] POST /archer/generate_link                              app.py:547
# -----------------------------------------------------------------------------
# Body:    { asin, label? }   (label defaults to asin)
# Returns: Archer API response (attribution_link / url / etc.)
# Errors:  400 if asin missing; 500 if generation fails

# -----------------------------------------------------------------------------
# [INT] GET /archer/product/<asin>                              app.py:566
# -----------------------------------------------------------------------------
# Returns one product. If image missing, hits Archer /get_single_product live
# and writes image back to SQLite cache via _db_connect().
# 404 if not found.

# -----------------------------------------------------------------------------
# [INT] POST /archer/generate_caption                           app.py:603
# -----------------------------------------------------------------------------
# Body:    { products: "<csv-of-product-names>" }
# Returns: { caption: str }   (Claude-generated)

# -----------------------------------------------------------------------------
# [INT] POST /archer/collage/save                               app.py:623
# -----------------------------------------------------------------------------
# Body:    {
#   slug, products:[{asin,...}], layout?, theme?, caption?, direct_to_amazon?
# }
# Side:    auto-generates Archer attribution links for products lacking one;
#          INSERT OR REPLACE into collages table.
# Returns: { url:"/shop/<slug>", slug }

# -----------------------------------------------------------------------------
# [INT] GET /archer/collages                                    app.py:657
# -----------------------------------------------------------------------------
# Returns last 20 collages: { collages:[{slug,theme,layout,created_at,
#                                        click_count,product_count}] }

# -----------------------------------------------------------------------------
# [INT] GET /shop/<slug>                                        app.py:680
# -----------------------------------------------------------------------------
# Renders shop_landing.html for a public shoppable page.
# 404 if slug not found.

# -----------------------------------------------------------------------------
# [INT] POST /archer/track_click                                app.py:700
# -----------------------------------------------------------------------------
# Body:    { asin, slug, fbclid, attribution_url }
# Side:    INSERT into click_log; increments collages.click_count for slug.
# Returns: { ok: true }

# -----------------------------------------------------------------------------
# [INT] GET /archer/image_proxy                                 app.py:718
# -----------------------------------------------------------------------------
# Query:   url=<remote img>, filename=<download name>
# Returns: image bytes with Content-Disposition: attachment
# Errors:  400 if invalid url; 500 on fetch fail

# -----------------------------------------------------------------------------
# [INT] POST /archer/generate_ad_copy                           app.py:743
# -----------------------------------------------------------------------------
# Body:    { products, campaign_type, routing, slug, product_asins:[asin,...] }
# Returns: { variants: [ {headline, primary_text, cta, attribution_url, label}, x3 ] }
# Notes:   Claude returns JSON; first product_asin is used to mint Archer links.

# -----------------------------------------------------------------------------
# [INT] POST /archer/ads/save                                   app.py:796
# -----------------------------------------------------------------------------
# Body:    { slug, products, variants, campaign_type, routing,
#            spend_budget, forecast_roas }
# Side:    Auto-mints attribution links for variants missing one;
#          INSERT OR REPLACE into campaigns table (status='draft').
# Returns: { ok:true, slug }

# -----------------------------------------------------------------------------
# [INT] GET /archer/ads/campaigns                               app.py:835
# -----------------------------------------------------------------------------
# Returns last 20 campaigns: { campaigns:[{slug,campaign_type,routing,
#         product_count,forecast_roas,status,created_at}] }


# =============================================================================
# 5. INTERNAL ROUTES — URLGENIUS
# =============================================================================

# -----------------------------------------------------------------------------
# [INT] POST /urlgenius/smart_link                              app.py:893
# -----------------------------------------------------------------------------
# Body: {
#   asin: str (required),
#   network: 'amazon' | 'archer' | 'levanta' (default 'amazon'),
#   placement: {                              # all required except term
#     source: 'facebook'|'instagram'|'tiktok'|'email'|'steph-ai',
#     medium: see VALID_PLACEMENTS below,
#     campaign: str,
#     term?: str
#   },
#   force_new?: bool                          # bypass dedup registry
# }
# Validation:
#   VALID_PLACEMENTS = {
#     facebook:  [organic, paid],
#     instagram: [organic, paid],
#     tiktok:    [organic, paid],
#     email:     [newsletter],
#     'steph-ai':[ai-agent],
#   }
#   utm_content is AUTO-DERIVED from network — never accepted from caller:
#   NETWORK_CONTENT = { amazon:'amazon-assoc', archer:'archer', levanta:'levanta' }
# Flow:
#   1. Validate placement.source × placement.medium
#   2. Build affiliate_url per network
#       amazon  → https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}
#       archer  → ArcherAPI.generate_link(asin, label=...)
#       levanta → LevantaAPI.create_product_link(asin)
#   3. Wrap with URLGenius.create_link() (registry-deduped)
# Returns: {
#   genius_url, affiliate_url, network, label, utm:{...},
#   urlgenius:bool, from_registry:bool, link_id?
# }
# Errors: 400 (validation), 500 (network failure)
# Test:
#   curl -sX POST localhost:5000/urlgenius/smart_link \
#     -H 'content-type: application/json' -d '{
#       "asin":"B0C84VRPWL","network":"amazon",
#       "placement":{"source":"instagram","medium":"organic","campaign":"spring26"}
#     }'

# -----------------------------------------------------------------------------
# [INT] GET /urlgenius/test                                     app.py:1025
# -----------------------------------------------------------------------------
# Smoke test against URLGenius using a hardcoded ASIN.
# Returns: { status:'connected', result:{...} } or { error }

# -----------------------------------------------------------------------------
# [INT] POST /urlgenius/create_link                             app.py:1042
# -----------------------------------------------------------------------------
# Body:    { url, utm_source?, utm_medium?, utm_campaign?, utm_content? }
# Returns: raw URLGenius API response

# -----------------------------------------------------------------------------
# [INT] GET /urlgenius/links                                    app.py:1065
# -----------------------------------------------------------------------------
# Returns: full list of URLGenius links for this account.


# =============================================================================
# 6. INTERNAL ROUTES — LEVANTA
# =============================================================================

# -----------------------------------------------------------------------------
# [INT] GET /levanta/diag                                       app.py:265
# -----------------------------------------------------------------------------
# Returns sample of products+brands (3 each) plus their key shapes,
# for verifying field names. 400 if no LEVANTA_API_KEY.

# -----------------------------------------------------------------------------
# [INT] POST /levanta/generate_link                             app.py:1079
# -----------------------------------------------------------------------------
# Body:    { asin, label? }   (label maps to Levanta sourceId)
# Returns: raw Levanta /links response

# -----------------------------------------------------------------------------
# [INT] GET /levanta/deals                                      app.py:1095
# -----------------------------------------------------------------------------
# Returns: raw Levanta /deals response (live deals feed).

# -----------------------------------------------------------------------------
# [INT] POST /webhooks/levanta                                  app.py:1105
# -----------------------------------------------------------------------------
# Headers: x-levanta-hmac-sha256: <hex>
# Body:    { type, data:{...} }
# Auth:    HMAC-SHA256 against LEVANTA_WEBHOOK_SECRET (compared with hmac.compare_digest)
# Handled event types:
#   product.access.gained  → log new accessible ASIN + commission
#   link.disabled          → warn
#   product.added          → log new catalog ASIN
#   product.removed        → warn
# Returns: { received: true } (always 200 if signature valid)
# Errors:  401 on bad signature


# =============================================================================
# 7. INTERNAL ROUTES — STATIC PAGES
# =============================================================================

GET /                      # index.html
GET /plan                  # steph-ai-plan.html
GET /architecture          # steph-architecture.html
GET /connections           # steph-connection-map.html
GET /archer/products       # templates/archer_products.html (main UI)
GET /archer/collage        # templates/archer_collage.html
GET /archer/ads            # templates/archer_ads.html


# =============================================================================
# 8. DATA FILES (file-based "endpoints")
# =============================================================================

data/archer_catalog.db              # SQLite — products, collages, click_log, campaigns, cache_meta
data/matched_asins.json             # output of asin_match_scan
data/scan_meta.json                 # last scan metadata
data/network_cache_levanta.json     # Levanta catalog snapshot (live → cache fallback)
data/urlgenius_registry.json        # URLGenius dedup registry
data/earnings_latest.csv            # latest uploaded Amazon earnings CSV
data/2025-Q12026 amazon asin earnings.csv   # legacy filename fallback
data/Archer Full Catalog 2026.csv   # static Archer catalog seed
data/levanta_matches.json           # output of /archer/levanta_match_scan
data/Amazon_Earnings_2026.csv       # input to /archer/levanta_match_scan


# =============================================================================
# 9. KNOWN GOTCHAS / DOC DRIFT
# =============================================================================
#
# - Constant naming drift: prior audit referenced VALID_MEDIUMS and a
#   facebook→[group,page,messenger,ads] map. The CURRENT code uses
#   VALID_PLACEMENTS with [organic,paid] for FB/IG/TikTok and a different
#   NETWORK_CONTENT mapping (amazon-assoc / archer / levanta). Any test
#   harness should target VALID_PLACEMENTS, not VALID_MEDIUMS.
#
# - /archer/levanta_match_scan reads data/Amazon_Earnings_2026.csv (NOT
#   earnings_latest.csv) and skips the first row (report title). Different
#   contract from /archer/upload_earnings.
#
# - Archer token lifetime is 55 minutes (5-min safety buffer below the 1h grant).
#
# - All ArcherAPI DB connections go through _db_connect(timeout=30), which sets
#   PRAGMA journal_mode=WAL. Hand-rolled sqlite3.connect() calls should be
#   avoided.
#
# - URLGenius writes the dedup registry on every successful create. force_new=true
#   bypasses the registry but still updates it.
#
# - When LEVANTA_API_KEY is unset, /urlgenius/smart_link with network='levanta'
#   will 500. /levanta/diag returns 200 with {error} (no status code change).
#
# - load_earnings_csv() handles '-', 'N/A', and empty strings via the inner
#   clean_num() helper. The legacy /archer/levanta_match_scan handler does
#   NOT use clean_num — it uses bare float()/int() with try/except.
