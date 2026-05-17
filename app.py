import os
import re
import json
import logging
import sqlite3
import time
import tempfile
import threading
import requests as req
from flask import Flask, send_from_directory, request, jsonify, render_template, Response, redirect, url_for, session
from datetime import timedelta as _admin_timedelta
from dotenv import load_dotenv
import anthropic

import db_schema
import storefront_chat
from product_api import ProductResolver, detect_category
from prompts import (
    build_chat_prompt, build_chat_products,
    build_caption_prompt,
)

load_dotenv()  # loads .env locally; Replit Secrets override in production

app = Flask(__name__)

# ── Admin session auth ────────────────────────────────────────────────────────
# Single shared password for now (default 'dan', override via ADMIN_PASSWORD).
# Auth state lives in a signed Flask session cookie (signed with SECRET_KEY).
# API mutation routes also accept the legacy X-Walmart-Trends-Admin-Token
# header for backwards compatibility with any external automation.
#
# Production fail-closed posture: when running under a real database
# (DATABASE_URL set AND FLASK_ENV != 'development'), SECRET_KEY and
# ADMIN_PASSWORD are REQUIRED. If either is missing, the app keeps serving
# /healthz and public storefront routes, but every admin path returns 503
# with a clear "missing production admin config" message. See
# _admin_config_missing() and the loud startup warning below.
_RAW_SECRET_KEY = os.environ.get('SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY')
_RAW_ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')


def _is_production_env() -> bool:
    """True when the app is running against a production-style backend.

    We treat 'production' as: a DATABASE_URL is set AND FLASK_ENV is not
    explicitly 'development'. This catches Replit deployments (which auto-
    inject DATABASE_URL) without requiring the operator to set FLASK_ENV.
    """
    if (os.environ.get('FLASK_ENV') or '').lower() == 'development':
        return False
    return bool(os.environ.get('DATABASE_URL'))


def _admin_config_missing() -> list:
    """Return a list of REQUIRED env vars that are missing in production.

    Empty list in dev or when fully configured. Non-empty in prod means
    admin paths should refuse to serve.
    """
    if not _is_production_env():
        return []
    missing = []
    if not (_RAW_SECRET_KEY or '').strip():
        missing.append('SECRET_KEY')
    if not (_RAW_ADMIN_PASSWORD or '').strip():
        missing.append('ADMIN_PASSWORD')
    return missing


# Flask still needs *some* secret to sign session cookies even during the
# startup warning window; signed cookies are not useful here because we
# refuse all admin paths anyway, but Flask raises if secret_key is empty.
app.secret_key = _RAW_SECRET_KEY or 'echotribe-dev-secret-please-change-in-prod'
app.permanent_session_lifetime = _admin_timedelta(days=30)
ADMIN_PASSWORD = _RAW_ADMIN_PASSWORD or 'dan'

# Loud startup warning so the missing config is visible in Replit logs the
# first time the app boots. This runs once at module import time.
_STARTUP_MISSING = _admin_config_missing()
if _STARTUP_MISSING:
    logging.error(
        "[BOOT] PRODUCTION ADMIN CONFIG MISSING: %s. Admin routes will return "
        "503 until these env vars are set. /healthz and public shop routes "
        "remain available. Set these in Replit Secrets, then redeploy.",
        ', '.join(_STARTUP_MISSING),
    )


# ── Admin guards (decorators) ────────────────────────────────────────────────
# These are defined high in the module so route handlers below can apply
# them as `@require_admin_api` / `@require_admin_page`. The underlying
# functions they call (_require_walmart_trends_admin, _require_admin_page)
# live further down with the rest of the auth helpers — they're resolved
# at request time, not at decorator-definition time, so forward references
# are fine.

def require_admin_api(view):
    """Decorator: refuse JSON admin endpoints unless caller is authed.

    Used on /archer/* JSON routes (audit follow-up 0.5) to enforce
    session-OR-header auth in a single line instead of three inline.
    """
    from functools import wraps as _wraps

    @_wraps(view)
    def wrapped(*args, **kwargs):
        guard = _require_walmart_trends_admin()
        if guard:
            return guard
        return view(*args, **kwargs)
    return wrapped


def require_admin_page(view):
    """Decorator: refuse admin HTML pages unless caller has a session.

    Used on /archer/* HTML pages (audit follow-up 0.5) so the route
    redirects unauthenticated visitors to /admin/login instead of
    rendering the page.
    """
    from functools import wraps as _wraps

    @_wraps(view)
    def wrapped(*args, **kwargs):
        guard = _require_admin_page()
        if guard:
            return guard
        return view(*args, **kwargs)
    return wrapped

THEMES = {
    'coral':    {'bg': '#fff5f5', 'accent': '#ff6b6b', 'btn': '#e85d26', 'text': '#1a1a17'},
    'ocean':    {'bg': '#e8f4f8', 'accent': '#2e7dd4', 'btn': '#0a6b52', 'text': '#0f4a8a'},
    'lavender': {'bg': '#f5f0ff', 'accent': '#a78bfa', 'btn': '#ec4899', 'text': '#4c1d95'},
    'forest':   {'bg': '#f0f7f2', 'accent': '#27693a', 'btn': '#8a5510', 'text': '#1a2e1a'},
    'midnight': {'bg': '#1a1a17', 'accent': '#e8e5dc', 'btn': '#888780', 'text': '#e8e5dc'},
    'peach':    {'bg': '#fdf6f0', 'accent': '#e85d26', 'btn': '#8a5510', 'text': '#1a1a17'},
    'clean':    {'bg': '#ffffff', 'accent': '#1a1a17', 'btn': '#2e7dd4', 'text': '#1a1a17'},
    'bold':     {'bg': '#fff8f6', 'accent': '#e85d26', 'btn': '#a02828', 'text': '#1a1a17'},
    'sage':     {'bg': '#f0f4f0', 'accent': '#3a7a4a', 'btn': '#2d6b3c', 'text': '#1a1a17'},
    'sand':     {'bg': '#fdf8f0', 'accent': '#8a6a3a', 'btn': '#7a5a2a', 'text': '#1a1a17'},
    # "Mommy & Me Collective" — warm pink palette matched to the default
    # creator's brand_label (db_schema.DEFAULT_CREATOR). Tweak hex values
    # in coordination with brand guidelines before launch.
    'mommyme':  {'bg': '#fef6ee', 'accent': '#e85d8f', 'btn': '#c44a78', 'text': '#3a2a2a'},
}

PIXEL_ID = os.environ.get('FB_PIXEL_ID', '1559451780790812')

# ── shop.echotribe.ai subdomain ──────────────────────────────────────────────
# When DNS for shop.echotribe.ai points to this Flask app, requests like
# https://shop.echotribe.ai/summer-essentials are rewritten so the existing
# /shop/<slug> handler renders. Cleaner share URLs without a /shop/ prefix.
SHOP_SUBDOMAIN = os.environ.get('SHOP_SUBDOMAIN', 'shop.echotribe.ai').lower()


def _fmt_date(v) -> str:
    """Format a date value (datetime obj or ISO string) to YYYY-MM-DD string."""
    if v is None:
        return ''
    if hasattr(v, 'date'):
        return str(v.date())
    return str(v)[:10]


_SCHEMA_READY = False
_SCHEMA_READY_LOCK = threading.Lock()


def _ensure_schema_ready() -> None:
    """Create/patch schema lazily without blocking app import or /healthz."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_READY_LOCK:
        if _SCHEMA_READY:
            return
        started = time.time()
        db_schema.init_schema()
        db_schema.seed_default_creator()
        _SCHEMA_READY = True
        logging.info("[BOOT] schema ready in %.2fs", time.time() - started)


def _public_shop_nav(active: str = '') -> list[dict]:
    base = f'https://{SHOP_SUBDOMAIN}'
    return [
        {'key': 'collections', 'label': 'Collections', 'href': f'{base}/collections'},
        {'key': 'trends', 'label': 'Trends', 'href': f'{base}/trends'},
        {'key': 'posts', 'label': 'Social Posts', 'href': f'{base}/posts'},
    ]


@app.before_request
def _route_shop_subdomain():
    """If host == shop.echotribe.ai, rewrite GET requests to public-only routes.

    Public surface on the shop subdomain:
      GET  /                      → shop_directory()         (creator-aware index)
      GET  /sitemap.xml           → shop_sitemap()
      GET  /robots.txt            → shop_robots()
      GET  /<slug>                → shop_landing(slug)
      POST /api/shop/chat         → storefront chat endpoint (passthrough)
      POST /archer/track_click    → tracking endpoint (passthrough)
      *    /static/*              → passthrough for assets
    Anything else 404s on the public subdomain.
    """
    host = (request.host or '').split(':')[0].lower()
    if host != SHOP_SUBDOMAIN:
        return  # normal routing for the dashboard host

    path = request.path or '/'

    # Passthroughs the public surface itself needs
    if path == '/api/shop/chat':
        return
    if path == '/archer/track_click':
        return
    if path.startswith('/static/'):
        return

    if request.method == 'GET':
        if path == '/' or path == '':
            return shop_directory()
        if path == '/collections':
            return shop_directory()
        if path == '/trends':
            return shop_trends()
        if path == '/posts':
            return shop_posts()
        if path == '/sitemap.xml':
            return shop_sitemap()
        if path == '/robots.txt':
            return shop_robots()

        slug = path.lstrip('/').split('/')[0]
        if not slug or slug.startswith('favicon'):
            return jsonify({'error': 'Not found'}), 404
        return shop_landing(slug)

    return jsonify({'error': 'Not found'}), 404


# ── Chat prompt cache (2-hour TTL) ───────────────────────────────────────────
_CHAT_CACHE: dict = {'prompt': None, 'products': None, 'expires': 0.0}

def _get_chat_context():
    """Return (system_prompt_str, chat_products_list), refreshing every 2 hours."""
    global _CHAT_CACHE
    if _CHAT_CACHE['prompt'] and time.time() < _CHAT_CACHE['expires']:
        return _CHAT_CACHE['prompt'], _CHAT_CACHE['products']

    from product_api import ArcherAPI
    archer_products = []
    try:
        a = ArcherAPI()
        archer_products = a._load_matched_json()[:15]
    except Exception as e:
        logging.warning(f'[CHAT] Failed to load Archer products for prompt: {e}')

    prompt = build_chat_prompt(archer_products)
    chat_products = build_chat_products(archer_products)
    _CHAT_CACHE = {'prompt': prompt, 'products': chat_products, 'expires': time.time() + 7200}
    return prompt, chat_products

product_resolver = ProductResolver([])

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    print(f"[CHAT] Received message: {user_message[:50]}...")
    if not user_message:
        return jsonify({'error': 'message is required'}), 400

    try:
        _ensure_schema_ready()
        system_prompt, chat_products = _get_chat_context()
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=256,
            system=system_prompt,
            messages=[{'role': 'user', 'content': user_message}],
        )
        reply = message.content[0].text
        print(f"[REPLY] Claude response length: {len(reply)} | Contains PRODUCTS: {'PRODUCTS:' in reply} | Contains SEARCH: {'SEARCH:' in reply}")

        # Parse product recommendations from response
        products = []
        text_reply = reply

        if 'PRODUCTS:' in reply:
            parts = reply.split('PRODUCTS:')
            text_reply = parts[0].strip()
            product_ids_str = parts[1].strip()

            try:
                product_ids = [int(pid.strip()) for pid in product_ids_str.split(',')]
                products = [chat_products[pid] for pid in product_ids if 0 <= pid < len(chat_products)]
            except (ValueError, IndexError):
                pass
        
        elif 'SEARCH:' in reply:
            parts = reply.split('SEARCH:')
            text_reply = parts[0].strip()
            search_query = parts[1].strip()
            
            category = detect_category(search_query)
            
            try:
                resolved_products = product_resolver.resolve(search_query, category, max_results=3)
                products = resolved_products
            except Exception as e:
                products = []
        
        else:
            # Fallback: If Claude didn't include PRODUCTS: or SEARCH: but the query suggests searching,
            # detect and trigger search automatically
            # Common search indicators: "show me", "find", "cheap", "budget", "kitchen", "gadgets", "decor", etc.
            search_indicators = ['show me', 'find', 'search for', 'look for', 'what about', 'kitchen', 'gadget', 
                               'decor', 'furniture', 'cheap', 'budget', 'affordable', 'inexpensive', 'under $']
            
            has_search_indicator = any(indicator in user_message.lower() for indicator in search_indicators)
            print(f"[DEBUG] No PRODUCTS/SEARCH in reply. Has search indicator: {has_search_indicator}")
            
            if has_search_indicator:
                # User is likely asking for something to search for
                category = detect_category(user_message)
                print(f"[DEBUG] Detected category: {category}")
                try:
                    resolved_products = product_resolver.resolve(user_message, category, max_results=3)
                    products = resolved_products
                    print(f"🔍 Auto-triggered search for: {user_message} | Found {len(products)} products")
                except Exception as e:
                    print(f"[ERROR] Product resolution error: {e}")
                    import traceback
                    traceback.print_exc()
                    products = []
        
        return jsonify({
            'reply': text_reply,
            'products': products
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _format_display_price(raw) -> str:
    """Normalize storefront price display to include '$' for plain USD numerics."""
    if raw is None:
        return ''
    s = str(raw).strip()
    if not s:
        return ''
    if '$' in s:
        return s
    # Common numeric forms: 19, 19.9, 19.99
    if re.fullmatch(r'\d+(\.\d{1,2})?', s):
        return f'${s}'
    # Common ranges: 19.99-24.99 or 19 - 24
    m = re.fullmatch(r'(\d+(?:\.\d{1,2})?)\s*[-–]\s*(\d+(?:\.\d{1,2})?)', s)
    if m:
        return f'${m.group(1)}-${m.group(2)}'
    return s


@app.route('/api/shop/chat', methods=['POST'])
def shop_chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    creator_id = (data.get('creator_id') or 'everydaywithsteph').strip() or 'everydaywithsteph'
    slug = (data.get('slug') or '').strip().lower()
    session_id = storefront_chat.ensure_session_id(data.get('session_id'))
    if not user_message:
        return jsonify({'error': 'message is required'}), 400

    try:
        history = storefront_chat.load_chat_history(creator_id, session_id)
        candidates = storefront_chat.retrieve_candidates(
            creator_id,
            user_message,
            current_slug=slug,
            history=history,
            limit=20,
        )
        if not candidates:
            return jsonify({
                'reply': "I don’t have product data loaded yet. Please check back in a bit 💕",
                'products': [],
                'session_id': session_id,
            })

        creator = db_schema.get_creator(creator_id)
        catalog = storefront_chat.format_candidates_for_prompt(candidates)
        history_text = storefront_chat.format_history_for_prompt(history)
        raw = ''
        if os.environ.get('ANTHROPIC_API_KEY'):
            client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
            msg = client.messages.create(
                model='claude-sonnet-4-20250514',
                max_tokens=320,
                system=(
                    "You are a public shopping assistant for a creator storefront. "
                    "Use the creator context and short conversation history to keep follow-up questions connected. "
                    "Recommend only from the provided candidate products. "
                    "Prioritize query relevancy, then exact title/brand/category overlap, then popularity/click signals. "
                    "Use current page only as context, not as a product filter. "
                    "Keep tone concise, friendly, and shopper-facing. "
                    "Return exactly two lines:\n"
                    "REPLY: <short shopper-facing text>\n"
                    "PRODUCTS: <comma-separated candidate indexes (max 3)>"
                ),
                messages=[{
                    'role': 'user',
                    'content': (
                        f"Creator: {creator.get('display_name') or creator_id} {creator.get('handle') or ''}\n"
                        f"Creator voice/context: {creator.get('voice_prompt') or ''}\n"
                        f"Conversation history:\n{history_text}\n\n"
                        f"Current shopper query: {user_message}\n"
                        f"Current landing slug: {slug or '(none)'}\n"
                        f"Candidates:\n{catalog}"
                    )
                }],
            )
            raw = (msg.content[0].text or '').strip()
        else:
            raw = "REPLY: Here are the best matches I found from this creator's shop.\nPRODUCTS: 0,1,2"

        text_reply = "Here are a few picks you might love."
        parsed_reply, indexes = storefront_chat.parse_product_indexes(raw, len(candidates), max_items=3)
        text_reply = parsed_reply or text_reply
        picked = [candidates[idx] for idx in indexes] or candidates[:3]

        out_products = []
        for p in picked[:3]:
            out_products.append(storefront_chat.response_product(
                p,
                creator,
                current_slug=slug,
                make_smart_link=_make_smart_link,
            ))

        storefront_chat.append_chat_turn(creator_id, session_id, user_message, text_reply, out_products)

        return jsonify({'reply': text_reply, 'products': out_products, 'session_id': session_id})
    except Exception as e:
        logging.error(f"[SHOP_CHAT] failed: {e}")
        return jsonify({
            'reply': "I hit a snag, but here are top picks from this creator.",
            'products': [],
            'session_id': session_id,
            'error': str(e),
        }), 500

@app.route('/')
def index():
    return redirect(url_for('hub'))


@app.route('/healthz')
def healthz():
    return 'ok', 200


@app.route('/hub')
def hub():
    guard = _require_admin_page()
    if guard:
        return guard
    return render_template('hub.html', shop_subdomain=SHOP_SUBDOMAIN)

# ARCHIVED — see /archive/routes/

# /dashboard (the EchoTribe ad-ops scorecard) and templates/dashboard.html
# removed in the Shop-MomandMe strip-down (2026-05-17). The template's
# four data-fetches (/urlgenius/links, /archer/ads/campaigns,
# /archer/campaigns, /archer/ads) all targeted ad-ops surfaces that
# this strip-down removes. The route was not on the runbook's deferred-work
# list but its template's purpose was ad-ops only — flagged as scope
# expansion in the PR description.
#
# /dashboard/upload_csv stays — it's the Amazon-earnings CSV ingest, used
# by /insights data plumbing. P0.2 (Insights rebuild) will surface upload
# UI inside the new framework page.

@app.route('/dashboard/upload_csv', methods=['POST'])
@require_admin_api
def dashboard_upload_csv():
    """Upload Amazon Associates earnings CSV.

    Phase 2A: persists rows into earnings_amazon so /insights can do
    click-weighted revenue reconciliation by ASIN. Existing UI behavior
    (returning the top-10 products) is preserved.

    Optional form fields:
      creator_id   — defaults to 'everydaywithsteph'
      period_start — ISO date 'YYYY-MM-DD' (defaults to today)
      period_end   — ISO date 'YYYY-MM-DD' (defaults to today)
    """
    import csv, io
    from datetime import date as _date
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.csv'):
        return jsonify({'error': 'File must be a .csv'}), 400

    creator_id   = (request.form.get('creator_id') or 'everydaywithsteph').strip()
    period_start = (request.form.get('period_start') or _date.today().isoformat()).strip()
    period_end   = (request.form.get('period_end')   or _date.today().isoformat()).strip()
    source_file  = f.filename

    try:
        text = f.read().decode('utf-8-sig')
        reader_io = io.StringIO(text)
        next(reader_io)  # skip report title row
        rows = list(csv.DictReader(reader_io))
        products = []
        persist_rows = []
        for row in rows:
            asin = (row.get('ASIN') or '').strip()
            if not asin:
                continue
            try:
                earnings = float(row.get('Total Earnings') or row.get('Revenue($)') or 0)
                units    = int(float(row.get('Items Shipped') or 0))
            except (ValueError, TypeError):
                earnings, units = 0.0, 0
            name = (row.get('Name') or row.get('Title') or asin).strip()
            products.append({
                'asin':           asin,
                'product_name':   name,
                'total_earnings': round(earnings, 2),
                'items_shipped':  units,
            })
            persist_rows.append((
                creator_id, asin, name, period_start, period_end,
                round(earnings, 2), units, source_file,
            ))

        # Persist to earnings_amazon for /insights reconciliation
        if persist_rows:
            try:
                from product_api import ArcherAPI
                conn = ArcherAPI()._db_connect()
                conn.executemany(
                    "INSERT INTO earnings_amazon "
                    "(creator_id, asin, product_name, period_start, period_end, "
                    " earnings, units, source_file) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    persist_rows,
                )
                conn.commit()
                conn.close()
                logging.info(
                    f"[CSV_UPLOAD] Persisted {len(persist_rows)} rows for "
                    f"{creator_id} ({period_start}..{period_end}) from {source_file}"
                )
            except Exception as _e:
                logging.warning(f"[CSV_UPLOAD] persist failed: {_e}")

        products.sort(key=lambda p: p['total_earnings'], reverse=True)
        return jsonify({
            'products': products[:10],
            'persisted': len(persist_rows),
            'creator_id': creator_id,
            'period_start': period_start,
            'period_end': period_end,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# /archer/products HTML page removed in the Shop-MomandMe strip-down
# (2026-05-17). The JSON variant /archer/product/<asin> (KEEP) is the
# canonical product-lookup endpoint going forward.

# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/

@app.route('/archer/search')
@require_admin_api
def archer_search():
    """Search Archer and/or Levanta catalogs. Supports network=archer|levanta|both."""
    from product_api import ArcherAPI, LevantaAPI
    from utils.asin import extract_asin
    q = request.args.get('q', '').strip()

    # If the query looks like an ASIN or product URL, resolve to ASIN and redirect
    resolved_asin = extract_asin(q) if q else None
    if resolved_asin:
        from flask import redirect, url_for
        return redirect(url_for('archer_get_product', asin=resolved_asin))

    category = request.args.get('category', '')
    min_commission = int(request.args.get('min_commission', 0))
    limit = min(int(request.args.get('limit', 20)), 200)
    offset = int(request.args.get('offset', 0))
    network = request.args.get('network', 'archer')

    results = []

    if network in ('archer', 'both'):
        a = ArcherAPI()
        archer_results = a.search_catalog(q, category=category or None, limit=limit)
        # Supplement from matched JSON when SQLite is sparse
        if len(archer_results) < limit:
            matched = a._load_matched_json()
            q_lower = q.lower() if q else ''
            existing_asins = {r['asin'] for r in archer_results}
            for p in matched:
                if p.get('asin') in existing_asins:
                    continue
                cat_lower = category.lower() if category else ''
                name_match = q_lower and (q_lower in (p.get('product_name') or '').lower() or
                    q_lower in (p.get('brand') or '').lower())
                cat_match = cat_lower and cat_lower in (p.get('archer_category') or '').lower()
                if name_match or cat_match or (not q_lower and not cat_lower):
                    archer_results.append({
                        'asin': p.get('asin'),
                        'product_name': p.get('product_name'),
                        'company_name': p.get('brand'),
                        'commission_payout': p.get('commission'),
                        'product_category': p.get('archer_category'),
                        'price': p.get('price'),
                        'avg_rating': p.get('rating'),
                        'steph_revenue': p.get('steph_revenue'),
                        'source': 'archer'
                    })
                if len(archer_results) >= limit:
                    break
        if min_commission > 0:
            archer_results = [p for p in archer_results if
                float((p.get('commission_payout') or '0').replace('%', '') or 0) >= min_commission]
        for p in archer_results:
            p['source'] = 'archer'
        results.extend(archer_results)

    levanta_formatted = []
    levanta_full_count = 0
    if network in ('levanta', 'both'):
        try:
            lv_cache_path = 'data/network_cache_levanta.json'
            lv_entries = []
            cache_data = None
            if os.path.exists(lv_cache_path):
                with open(lv_cache_path) as f:
                    cache_data = json.load(f)

            if not isinstance(cache_data, dict) or not cache_data:
                lv = LevantaAPI()
                if lv.api_key:
                    from product_api import LevantaNetworkMatcher
                    cache_data = LevantaNetworkMatcher().get_asin_data()
                else:
                    cache_data = {}

            for asin_val, meta in cache_data.items():
                try:
                    commission_val = float(meta.get('commission', 0) or 0)
                except (ValueError, TypeError):
                    commission_val = 0.0
                price_val = meta.get('price', '')
                lv_entries.append({
                    'asin': asin_val,
                    'product_name': meta.get('title', ''),
                    'company_name': meta.get('brand', ''),
                    'price': f"${price_val}" if price_val else '',
                    'commission_payout': meta.get('commission_pct', ''),
                    'image_encoded_string': meta.get('imageUrl', ''),
                    'product_category': meta.get('category', ''),
                    'source': 'levanta',
                    'rating': meta.get('rating', ''),
                    'ratingsTotal': meta.get('ratingsTotal', 0),
                    '_commission_raw': commission_val,
                })

            if q:
                q_lower = q.lower()
                lv_entries = [p for p in lv_entries if
                    q_lower in (p.get('product_name') or '').lower() or
                    q_lower in (p.get('company_name') or '').lower() or
                    q_lower in (p.get('asin') or '').lower()]
            if category:
                cat_lower = category.lower()
                lv_entries = [p for p in lv_entries if
                    cat_lower in (p.get('product_category') or '').lower()]
            if min_commission > 0:
                lv_entries = [p for p in lv_entries if
                    float((p.get('commission_payout') or '0').replace('%', '') or 0) >= min_commission]

            lv_entries.sort(key=lambda p: p.get('_commission_raw', 0), reverse=True)
            levanta_full_count = len(lv_entries)
            page_slice = lv_entries[offset:offset + limit]
            for p in page_slice:
                p.pop('_commission_raw', None)
            levanta_formatted = page_slice
        except Exception as e:
            logging.error(f"[LEVANTA] Catalog browse/search failed: {e}")

    archer_page = results[offset:offset + limit] if network in ('archer', 'both') else []
    combined = archer_page + levanta_formatted if network == 'both' else (archer_page or levanta_formatted)

    return jsonify({
        'products': combined[:limit],
        'archer': archer_page,
        'archer_total': len(results),
        'archer_catalog_total': 113835,
        'levanta': levanta_formatted,
        'levanta_total': levanta_full_count,
        'levanta_catalog_total': levanta_full_count,
    })

# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/

# /archer/generate_link removed in the Shop-MomandMe strip-down
# (2026-05-17). Used by archer_products.html (deleted). ArcherAPI.generate_link
# itself stays — /urlgenius/smart_link's archer branch and ArcherAPI's
# internal link-creation logic still call it.

@app.route('/archer/collage')
def archer_collage():
    guard = _require_admin_page()
    if guard:
        return guard
    return render_template('archer_collage.html')

@app.route('/archer/product/<asin>')
@require_admin_api
def archer_get_product(asin):
    from product_api import ArcherAPI
    from product_lookup_service import resolve_amazon_product
    a = ArcherAPI()
    try:
        product = resolve_amazon_product(asin, archer=a)
        if product:
            return jsonify({"product": product})
    except Exception as e:
        logging.error(f"[ARCHER] Product lookup failed for {asin}: {e}")
        return jsonify({"error": "Product not found"}), 404
    return jsonify({"error": "Product not found"}), 404

@app.route('/archer/generate_caption', methods=['POST'])
@require_admin_api
def archer_generate_caption():
    _ensure_schema_ready()
    data = request.get_json() or {}
    products_str = data.get('products', '')
    product_list = data.get('product_list', [])   # [{asin, product_name, brand, ...}, ...]
    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=200,
            system=build_caption_prompt(),
            messages=[{"role": "user", "content": f"Write a caption for these products: {products_str}"}]
        )
        caption = message.content[0].text.strip()

        # Auto-generate URLGenius organic links for each product (Task 6)
        from datetime import datetime as _dt
        mmdd = _dt.now().strftime('%m%d')
        links = []
        for p in product_list:
            asin = (p.get('asin') or '').strip()
            if not asin:
                continue
            brand_raw = (p.get('brand') or p.get('company_name') or 'brand').lower()
            brand_short = re.sub(r'[^a-z0-9]', '', brand_raw.split()[0])[:10]
            name_words = re.sub(r'[^a-z0-9 ]', '', (p.get('product_name') or p.get('name') or 'product').lower()).split()
            product_short = '-'.join(name_words[:2])[:15]
            campaign = f"{brand_short}-{product_short}-{mmdd}"
            link_result = _make_smart_link(
                asin=asin, network='amazon',
                utm_source='fb-group', utm_medium='organic',
                utm_campaign=campaign,
            )
            links.append({
                'asin': asin,
                'name': p.get('product_name') or p.get('name') or asin,
                'genius_url': link_result['genius_url'],
                'affiliate_url': link_result['affiliate_url'],
                'campaign': campaign,
            })

        link_lines = '\n'.join(f"• {l['name']}: {l['genius_url']}" for l in links)
        post_block = f"{caption}\n\n{link_lines}" if link_lines else caption

        return jsonify({"caption": caption, "links": links, "post_block": post_block})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# /archer/generate_organic_posts, /archer/generate_posts, and
# /archer/generate_campaign_package removed in the Shop-MomandMe
# strip-down (2026-05-17). All three were called only by ad-ops
# templates (archer_ads.html, archer_products.html, organic_posts.html)
# which this strip-down removed. The dead prompt builders
# (build_organic_posts_prompt, build_campaign_package_prompt,
# build_ad_copy_prompt) are dropped from the import line at the top
# of app.py in the same commit.
#
# /archer/generate_caption (KEEP) stays because archer_collage.html
# calls it for the collage builder's auto-caption feature.

@app.route('/archer/collage/save', methods=['POST'])
@require_admin_api
def archer_save_collage():
    """Save (or update) a collage.

    Branch 2B behavior change:
      status='draft'     → SKIP Archer attribution-link generation (saves API
                            quota during preview iteration)
      status='published' → Generate Archer links for any product missing one
                            (existing default behavior)
    Drafts are 404 publicly and viewable only via /shop/<slug>?preview=1.
    """
    import collection_service
    from product_api import ArcherAPI
    data = request.get_json() or {}
    try:
        existing = collection_service.get_collage(data.get('slug') or '')
        existing_types = (existing.get('campaign_types') if existing else None) or []
        if existing and ('walmart_trend' in existing_types or 'amazon_trend' in existing_types):
            return jsonify({
                'error': 'Trend-origin pages must be edited in the collection editor',
                'edit_url': f"/collections/{existing['slug']}/edit",
            }), 409
        archer = None

        def generate_link(asin, label):
            nonlocal archer
            if archer is None:
                archer = ArcherAPI()
            return archer.generate_link(asin, label=label)

        result = collection_service.save_collage(
            data,
            shop_subdomain=SHOP_SUBDOMAIN,
            link_generator=generate_link,
            campaign_types=['organic'],
        )
        return jsonify(result)
    except collection_service.CollectionServiceError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/archer/collage/publish', methods=['POST'])
@require_admin_api
def archer_collage_publish():
    """Promote a draft collage to published. Generates Archer attribution links
    for products that don't have them yet, then flips status to 'published'.

    Body: { "slug": "..." }
    """
    import collection_content as cc
    import collection_service
    from product_api import ArcherAPI
    data = request.get_json() or {}
    slug = data.get('slug')
    clean_slug = collection_service.normalize_slug(slug or '')
    if not clean_slug:
        return jsonify({'error': 'slug is required'}), 400

    try:
        draft_result = cc.publish_latest_draft_for_public_slug(clean_slug)
        if draft_result:
            return jsonify(draft_result)
    except cc.CollectionContentError as exc:
        return jsonify({'error': str(exc)}), 400

    archer = None

    def generate_link(asin, label):
        nonlocal archer
        if archer is None:
            archer = ArcherAPI()
        return archer.generate_link(asin, label=label)

    try:
        result = collection_service.publish_collage(
            clean_slug,
            shop_subdomain=SHOP_SUBDOMAIN,
            link_generator=generate_link,
        )
        return jsonify(result)
    except collection_service.CollectionServiceError as exc:
        status_code = 404 if str(exc) == 'collection not found' else 400
        return jsonify({'error': str(exc)}), status_code

@app.route('/archer/collage/archive', methods=['POST'])
@require_admin_api
def archer_collage_archive():
    """Soft-delete a collage by setting its status to 'archived'.

    Bypasses the trend-origin save guard intentionally — archiving is always
    allowed regardless of how the page originated.

    Body: { "slug": "..." }
    """
    import collection_content as cc
    import collection_service
    data = request.get_json() or {}
    slug = (data.get('slug') or '').strip()
    if not slug:
        return jsonify({'error': 'slug is required'}), 400
    clean_slug = collection_service.normalize_slug(slug)
    if not cc.archive_published_page(clean_slug):
        return jsonify({'error': 'collection not found'}), 404
    return jsonify({'ok': True, 'slug': clean_slug, 'status': 'archived'})


@app.route('/archer/collage/restore', methods=['POST'])
@require_admin_api
def archer_collage_restore():
    """Restore an archived collage to draft status without publishing it."""
    import collection_content as cc
    import collection_service
    data = request.get_json() or {}
    slug = (data.get('slug') or '').strip()
    if not slug:
        return jsonify({'error': 'slug is required'}), 400
    clean_slug = collection_service.normalize_slug(slug)
    if not cc.restore_archived_page(clean_slug):
        return jsonify({'error': 'collection not found'}), 404
    return jsonify({'ok': True, 'slug': clean_slug, 'status': 'draft'})


@app.route('/archer/collage/<slug>', methods=['GET'])
@require_admin_api
def archer_collage_get(slug):
    """Return one collection's full record (used by Ad Builder auto-load
    when ?collection=<slug> deep-link is hit, and by the Mode C edit flow)."""
    import collection_service
    out = collection_service.get_collage(slug)
    if not out:
        return jsonify({'error': 'not found'}), 404
    types = out.get('campaign_types') or []
    if 'walmart_trend' in types or 'amazon_trend' in types:
        out['editor_type'] = 'trend_collection'
        out['edit_url'] = f"/collections/{out['slug']}/edit"
    return jsonify({'collage': out})


@app.route('/archer/collages')
@require_admin_api
def archer_list_collages():
    import collection_service
    status = request.args.get('status') or 'published'
    try:
        collages = collection_service.list_collages(status=status, limit=50)
        for collage in collages:
            ctypes = collage.get('campaign_types') or []
            if 'walmart_trend' in ctypes or 'amazon_trend' in ctypes:
                collage['editor_type'] = 'trend_collection'
                collage['edit_url'] = f"/collections/{collage['slug']}/edit"
            else:
                collage['editor_type'] = 'collage'
                collage['edit_url'] = f"/archer/collage?collection={collage['slug']}"
    except collection_service.CollectionServiceError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'collages': collages})

@app.route('/shop/<slug>')
def shop_landing(slug):
    import collection_service
    is_preview = request.args.get('preview') == '1'
    collage = None
    if is_preview:
        import collection_content as cc
        draft = cc.get_latest_draft_for_public_slug(slug)
        if draft:
            collage = cc.collage_from_draft_for_shop(draft)
    if not collage:
        collage = collection_service.get_collage(slug)
    if not collage:
        return "Page not found", 404
    # Drafts only viewable via ?preview=1
    if (collage.get('status') or 'published') != 'published' and not is_preview:
        return "Page not found", 404

    products = collage.get('products') or []
    for p in products:
        p['price_display'] = _format_display_price(p.get('price_display') or p.get('price') or p.get('current_price') or '')
    collage['direct_to_amazon'] = bool(collage.get('direct_to_amazon'))

    # Resolve creator for branding + creator-specific FB pixel
    creator = db_schema.get_creator(collage.get('creator_id') or 'everydaywithsteph')
    pixel_id = creator.get('fb_pixel_id') or PIXEL_ID

    # SEO/OG metadata
    page_title = (
        collage.get('hero_title')
        or (collage.get('slug') or '').replace('-', ' ').title()
        or 'Shop'
    )
    page_description = (
        collage.get('hero_subtitle')
        or (collage.get('caption') or '')[:160]
        or f"Curated picks from {creator.get('handle', '@creator')}"
    )
    og_image = ''
    for p in products:
        if p.get('image_encoded_string'):
            og_image = p['image_encoded_string']
            break
    canonical_url = f"https://{SHOP_SUBDOMAIN}/{slug}"

    return render_template('shop_landing.html',
        collage=collage,
        products=products,
        themes=THEMES,
        pixel_id=pixel_id,
        creator=creator,
        shop_subdomain=SHOP_SUBDOMAIN,
        public_nav_items=_public_shop_nav('collections'),
        nav_active='collections',
        seo={
            'title':         page_title,
            'description':   page_description,
            'og_image':      og_image,
            'canonical_url': canonical_url,
        },
        is_preview=is_preview,
    )

@app.route('/shop/')
@app.route('/collections')
def shop_directory():
    """Public directory of all published collections at shop.echotribe.ai/

    Cross-creator listing with a creator badge per row. Works on both
    the dashboard host (/shop/) and the shop subdomain root (rewritten
    via _route_shop_subdomain).
    """
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT slug, theme, layout, products_json, created_at, click_count, "
        "       creator_id, status, campaign_types, hero_title, hero_subtitle, caption "
        "FROM collages "
        "WHERE COALESCE(status,'published') = 'published' "
        "ORDER BY created_at DESC "
        "LIMIT 200"
    ).fetchall()
    conn.close()

    creators_by_id = {c['id']: c for c in db_schema.list_creators()}

    items = []
    for r in rows:
        try:
            products = json.loads(r['products_json'] or '[]')
        except (json.JSONDecodeError, TypeError):
            products = []
        cover_img = ''
        for p in products:
            if p.get('image_encoded_string'):
                cover_img = p['image_encoded_string']
                break
        # Detect dominant retailer for chip display.
        seen = set()
        for p in products:
            if not isinstance(p, dict):
                continue
            for field in ('retailer', 'network', 'retailer_name'):
                v = str(p.get(field) or '').strip().lower()
                if v in ('walmart', 'amazon'):
                    seen.add(v)
                    break
        if seen == {'amazon'}:
            retailer_chip = 'amazon'
            retailer_label_value = 'Amazon'
        elif seen == {'walmart'}:
            retailer_chip = 'walmart'
            retailer_label_value = 'Walmart'
        else:
            retailer_chip = ''
            retailer_label_value = ''
        creator = creators_by_id.get(r['creator_id'] or 'everydaywithsteph', {})
        items.append({
            'slug':          r['slug'],
            'title':         r['hero_title'] or (r['slug'] or '').replace('-', ' ').title(),
            'subtitle':      r['hero_subtitle'] or (r['caption'] or '')[:160],
            'theme':         r['theme'],
            'cover_image':   cover_img,
            'product_count': len(products),
            'click_count':   r['click_count'] or 0,
            'created_at':    _fmt_date(r['created_at']),
            'creator_id':    r['creator_id'] or 'everydaywithsteph',
            'creator_handle': creator.get('handle') or '@creator',
            'creator_name':   creator.get('display_name') or 'Creator',
            'retailer':       retailer_chip,
            'retailer_label': retailer_label_value,
        })

    return render_template(
        'shop_directory.html',
        items=items,
        themes=THEMES,
        canonical_url=f'https://{SHOP_SUBDOMAIN}/collections',
        shop_subdomain=SHOP_SUBDOMAIN,
        public_nav_items=_public_shop_nav('collections'),
        nav_active='collections',
    )


@app.route('/shop/posts')
def shop_posts():
    """Public social-post feed (newest first), mobile-friendly card/grid."""
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, slug, asin, network, angle, copy, collection_slug, status, smart_link,
               product_name, product_brand, product_price, product_image,
               product_availability, product_rating, product_review_count,
               creator_id, created_at, posted_at
        FROM posts
        WHERE status IN ('approved', 'posted')
        ORDER BY COALESCE(posted_at, created_at) DESC
        LIMIT 400
        """
    ).fetchall()
    conn.close()

    from utils.retailer_labels import angle_label as _angle_label
    creators_by_id = {c['id']: c for c in db_schema.list_creators()}
    items = []
    for r in rows:
        creator = creators_by_id.get(r['creator_id'] or 'everydaywithsteph', {})
        copy = (r['copy'] or '').strip()
        _network = (r['network'] or 'amazon').lower()
        _retailer_display = 'Walmart' if _network == 'walmart' else ('Amazon' if _network == 'amazon' else '')
        items.append({
            'id': r['id'],
            'slug': r['slug'] or '',
            'asin': r['asin'] or '',
            'network': _network,
            'retailer_label': _retailer_display,
            'angle': r['angle'] or '',
            'angle_label': _angle_label(r['angle'] or ''),
            'copy': copy,
            'copy_excerpt': (copy[:180] + '…') if len(copy) > 180 else copy,
            'collection_slug': r['collection_slug'] or '',
            'status': r['status'] or 'draft',
            'smart_link': r['smart_link'] or '',
            'product_name': r['product_name'] or (r['asin'] or 'Product'),
            'product_brand': r['product_brand'] or '',
            'product_price': _format_display_price(r['product_price'] or ''),
            'product_image': r['product_image'] or '',
            'product_availability': r['product_availability'] or '',
            'product_rating': r['product_rating'],
            'product_review_count': r['product_review_count'],
            'creator_id': r['creator_id'] or 'everydaywithsteph',
            'creator_handle': creator.get('handle') or '@creator',
            'created_at': _fmt_date(r['created_at']),
            'posted_at': _fmt_date(r['posted_at']),
            'shop_url': f"https://{SHOP_SUBDOMAIN}/{r['collection_slug']}" if r['collection_slug'] else '',
            'cta_url': (
                f"https://{SHOP_SUBDOMAIN}/{r['collection_slug']}"
                if r['collection_slug']
                else (
                    r['smart_link']
                    or (
                        f"https://www.walmart.com/ip/{r['asin']}"
                        if (r['network'] or '').lower() == 'walmart'
                        else f"https://www.amazon.com/dp/{r['asin']}?tag={os.environ.get('AMAZON_ASSOC_TAG', 'mommymedeals-20')}"
                    )
                )
            ),
            'cta_label': (
                'Shop collection'
                if r['collection_slug']
                else ('Shop Walmart' if (r['network'] or '').lower() == 'walmart' else 'Shop Amazon')
            ),
        })

    return render_template(
        'shop_posts.html',
        items=items,
        canonical_url=f'https://{SHOP_SUBDOMAIN}/posts',
        shop_subdomain=SHOP_SUBDOMAIN,
        public_nav_items=_public_shop_nav('posts'),
        nav_active='posts',
    )




def _require_walmart_trends_admin():
    """Protect Walmart trend mutation endpoints.

    Accepts (in priority order):
      1. Server-issued admin session cookie (set via /admin/login).
      2. `X-Walmart-Trends-Admin-Token` header / `Authorization: Bearer <token>`.
      3. Dev/Replit-dev demo mode (per `_walmart_content_demo_allowed`).

    URL query-string tokens (?admin_token=) are NOT accepted — query
    strings leak through web-server logs, browser history, and Referer
    headers. Send the token as a header instead.

    Returns None on success; a (Response, status) tuple on failure.
    """
    # 0. Production fail-closed: refuse all admin paths if required config
    # is missing, regardless of caller credentials.
    missing = _admin_config_missing()
    if missing:
        return jsonify({
            'error': 'missing production admin config',
            'missing': missing,
        }), 503
    # 1. Session cookie — primary path going forward.
    if _admin_session_authed():
        return None
    # 2. Header-only token auth (legacy header support for cron jobs etc.).
    expected = (
        os.environ.get('WALMART_TRENDS_ADMIN_TOKEN')
        or os.environ.get('ADMIN_API_TOKEN')
        or os.environ.get('ADMIN_SECRET')
    )
    # 3. Dev/Replit-dev allowance (NO production .replit.app — see helper).
    if _walmart_content_demo_allowed():
        return None
    if not expected:
        return jsonify({'error': 'Walmart trends admin token is not configured'}), 503
    supplied = request.headers.get('X-Walmart-Trends-Admin-Token', '')
    auth = request.headers.get('Authorization', '')
    if not supplied and auth.lower().startswith('bearer '):
        supplied = auth.split(' ', 1)[1].strip()
    import hmac as hmac_lib
    if not supplied or not hmac_lib.compare_digest(supplied, expected):
        return jsonify({'error': 'unauthorized'}), 401
    return None


def _walmart_content_demo_allowed() -> bool:
    """Allow demo mutations without a secret only in local/Replit DEV contexts.

    NOTE: `.replit.app` (production Replit deploy domain) was intentionally
    REMOVED from this allow-list. Production deployments must authenticate
    via server session (password 'dan' or `ADMIN_PASSWORD`) or the explicit
    `X-Walmart-Trends-Admin-Token` header. The previous allow-list silently
    elevated every visitor to admin on production Replit URLs.
    """
    host = (request.host or '').split(':')[0].lower()
    return bool(
        os.environ.get('FLASK_ENV') == 'development'
        or os.environ.get('FLASK_DEBUG') == '1'
        or os.environ.get('REPLIT_DEV_DOMAIN')
        or host in {'localhost', '127.0.0.1'}
        or host.endswith('.replit.dev')
        or host.endswith('.repl.co')
        # Removed: host.endswith('.replit.app') — see docstring above.
    )


# ── Admin session helpers ────────────────────────────────────────────────────

def _admin_session_authed() -> bool:
    """True if the current request has a valid server-issued admin session."""
    return bool(session.get('admin_authed'))


def _admin_session_check_password(supplied: str) -> bool:
    """Constant-time compare against ADMIN_PASSWORD; case-insensitive."""
    import hmac as _hmac
    a = (supplied or '').strip().lower().encode('utf-8')
    b = (ADMIN_PASSWORD or '').strip().lower().encode('utf-8')
    return bool(a) and bool(b) and _hmac.compare_digest(a, b)


def _wants_json() -> bool:
    """Best-effort check whether this request expects a JSON (API) response."""
    if request.path.startswith('/api/') or request.path.startswith('/admin/walmart-trends/'):
        return True
    if request.is_json:
        return True
    accept = (request.headers.get('Accept') or '').lower()
    if 'application/json' in accept and 'text/html' not in accept:
        return True
    return False


def _require_admin_page():
    """Page-level admin guard. Returns a redirect response if not authed, else None.

    Use at the top of admin-only page handlers:
        guard = _require_admin_page()
        if guard:
            return guard
    """
    # Production fail-closed: refuse all admin pages if required config is
    # missing. Render plain text 503 (no template; missing SECRET_KEY may
    # break session-flash messages on the login page).
    missing = _admin_config_missing()
    if missing:
        return (
            "Admin unavailable: missing production config ("
            + ', '.join(missing)
            + "). Set these in Replit Secrets and redeploy.",
            503,
            {'Content-Type': 'text/plain; charset=utf-8'},
        )
    if _admin_session_authed():
        return None
    # Page-level guard: session-only. URL/header tokens were previously
    # honored here and would upgrade themselves to a 30-day session — a
    # major leakage risk because the URL or header travels through any
    # intermediate proxy log. JSON API guards still accept the header
    # for cron-job automation, but pages require /admin/login.
    # Redirect to login, preserving original path + query so we can bounce back.
    full_path = request.path
    if request.query_string:
        full_path = f"{request.path}?{request.query_string.decode('utf-8', errors='ignore')}"
    return redirect(url_for('admin_login', next=full_path))


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Single-password admin login. Sets a signed-cookie session on success."""
    # Production fail-closed: don't even render the login form if required
    # admin config is missing — submitting it would never authenticate.
    missing = _admin_config_missing()
    if missing:
        return (
            "Admin unavailable: missing production config ("
            + ', '.join(missing)
            + "). Set these in Replit Secrets and redeploy.",
            503,
            {'Content-Type': 'text/plain; charset=utf-8'},
        )
    error = None
    next_url = (request.args.get('next') or request.form.get('next') or '/hub').strip() or '/hub'
    # Don't allow open-redirects: only same-origin paths
    if not next_url.startswith('/'):
        next_url = '/hub'
    if request.method == 'POST':
        supplied = request.form.get('password') or ''
        if _admin_session_check_password(supplied):
            session.permanent = True
            session['admin_authed'] = True
            return redirect(next_url)
        error = 'Incorrect password.'
    if _admin_session_authed():
        return redirect(next_url)
    return render_template('admin_login.html', error=error, next=next_url)


@app.route('/admin/logout', methods=['GET', 'POST'])
def admin_logout():
    session.pop('admin_authed', None)
    return redirect(url_for('admin_login'))


def _require_walmart_admin_if_configured():
    """Protect content mutations while allowing explicit local/Replit demo mode.

    Accepts session cookie OR header token; in dev contexts allows the demo
    header (`X-Walmart-Content-Demo: 1`) without credentials.
    """
    # 0. Production fail-closed: refuse all admin paths if required config
    # is missing, regardless of caller credentials. Matches the posture
    # added to _require_walmart_trends_admin / _require_admin_page in
    # audit follow-up 0.4.
    missing = _admin_config_missing()
    if missing:
        return jsonify({
            'error': 'missing production admin config',
            'missing': missing,
        }), 503
    # Session cookie — primary path.
    if _admin_session_authed():
        return None
    expected = (
        os.environ.get('WALMART_TRENDS_ADMIN_TOKEN')
        or os.environ.get('ADMIN_API_TOKEN')
        or os.environ.get('ADMIN_SECRET')
    )
    supplied = request.headers.get('X-Walmart-Trends-Admin-Token', '')
    auth = request.headers.get('Authorization', '')
    if not supplied and auth.lower().startswith('bearer '):
        supplied = auth.split(' ', 1)[1].strip()
    if expected:
        import hmac as hmac_lib
        if supplied and hmac_lib.compare_digest(supplied, expected):
            return None
        if _walmart_content_demo_allowed() and request.headers.get('X-Walmart-Content-Demo') == '1':
            return None
        return jsonify({
            'error': 'unauthorized',
            'message': 'Admin session required. Log in at /admin/login or send X-Walmart-Trends-Admin-Token.',
        }), 401
    if _walmart_content_demo_allowed():
        return None
    return jsonify({'error': 'admin token is not configured for this production environment'}), 503


@app.route('/walmart/trending-now')
def walmart_trending_now_page():
    """Mobile-first Walmart What's Trending Now landing page.

    Public when no ?admin=1; requires admin session when ?admin=1.
    """
    admin_mode = request.args.get('admin') == '1'
    if admin_mode:
        guard = _require_admin_page()
        if guard:
            return guard

    from walmart_trends import get_trending_page_data, discover_workbooks

    admin_error = ''
    try:
        _ensure_schema_ready()
        started = time.time()
        data = get_trending_page_data()
        logging.info(
            "[TRENDING] loaded page data in %.2fs collections=%s",
            time.time() - started,
            len(data.get('collections') or []),
        )
    except Exception as exc:
        logging.exception("[TRENDING] failed to load page data")
        data = {'last_refreshed': '', 'collections': []}
        admin_error = f"Trending data could not load: {exc}"
    workbooks = []
    if admin_mode:
        try:
            workbooks = discover_workbooks()
        except Exception as exc:
            logging.exception("[TRENDING] failed to discover workbooks")
            msg = f"Workbook discovery failed: {exc}"
            admin_error = f"{admin_error} {msg}".strip() if admin_error else msg
    shop_nav_items = _public_shop_nav('trends')
    if admin_mode:
        shop_nav_items = [item for item in shop_nav_items if item['key'] == 'trends']
    return render_template(
        'walmart_trending_now.html',
        data=data,
        admin_mode=admin_mode,
        workbooks=workbooks,
        shop_subdomain=SHOP_SUBDOMAIN,
        public_nav_items=shop_nav_items,
        nav_active='trends',
        admin_error=admin_error,
    )


@app.route('/trends')
def shop_trends():
    """Public Walmart trends home for the shop subdomain/menu."""
    from walmart_trends import get_trending_page_data
    try:
        _ensure_schema_ready()
        data = get_trending_page_data()
    except Exception as exc:
        logging.warning("[WALMART_TRENDS] public trends unavailable: %s", exc)
        data = {'last_refreshed': '', 'collections': []}

    return render_template(
        'walmart_trending_now.html',
        data=data,
        admin_mode=False,
        shop_subdomain=SHOP_SUBDOMAIN,
        public_nav_items=_public_shop_nav('trends'),
        nav_active='trends',
        admin_error='',
    )


@app.route('/api/walmart/trending-now')
def walmart_trending_now_api():
    """JSON source for the Walmart What's Trending Now page."""
    from walmart_trends import get_trending_page_data

    _ensure_schema_ready()
    return jsonify(get_trending_page_data())


def _editor_retailer_context(collection):
    """Compute retailer label/copy bundle for the collection editor template."""
    from utils import retailer_labels as rl
    items = collection.get('items') if isinstance(collection, dict) else None
    # Prefer the collection's own retailer field (set by walmart_trends), then
    # fall back to inspecting items.
    direct = str((collection or {}).get('retailer') or '').strip().lower()
    retailer = direct if direct in ('walmart', 'amazon') else rl.collection_retailer(items)
    label = rl.retailer_label({'retailer': retailer}) if retailer else ''
    return {
        'retailer_key': retailer,                 # 'walmart' | 'amazon' | ''
        'retailer_label': label,                  # 'Walmart' | 'Amazon' | ''
        'retailer_label_or_finds': label or 'creator',
        'slug_prefix': retailer or 'trend',
        'default_cta': rl.collection_cta_default(items),
        'finds_label': f"{label} finds" if label else "creator finds",
    }


def _render_collection_create_post(collection_slug):
    """Shared handler for the (retailer-agnostic) create-post editor."""
    _ensure_schema_ready()
    import collection_content as cc

    collection = cc.get_walmart_collection(collection_slug)
    if not collection:
        return "Collection not found", 404
    creator_id = (request.args.get('creator_id') or 'everydaywithsteph').strip()
    existing_draft = cc.get_latest_draft_for_source_collection(collection_slug, creator_id)
    if existing_draft and existing_draft.get('product_snapshot'):
        products = existing_draft.get('product_snapshot') or []
        product_count = len(products)
    else:
        products = collection.get('items', []) or []
        product_count = len(products)
    rctx = _editor_retailer_context(collection)
    default_public_slug = cc.slugify(f"{rctx['slug_prefix']}-{collection.get('name') or collection_slug}")
    return render_template(
        'walmart_collection_create_post.html',
        collection=collection,
        products=products,
        product_count=product_count,
        creator_id=creator_id,
        default_public_slug=default_public_slug,
        demo_auth_allowed=_walmart_content_demo_allowed(),
        existing_draft=existing_draft,
        editor_mode='create',
        shop_subdomain=SHOP_SUBDOMAIN,
        retailer_ctx=rctx,
    )


def _render_collection_page_edit(public_slug):
    """Shared handler for the (retailer-agnostic) page editor."""
    _ensure_schema_ready()
    import collection_content as cc

    draft = cc.get_latest_draft_for_public_slug(public_slug)
    if not draft:
        return "Page draft not found", 404
    collection_slug = draft.get('source_collection_slug') or ''
    collection = cc.get_walmart_collection(collection_slug) if collection_slug else None
    if not collection:
        collection = cc.collection_from_draft_snapshot(draft)
    creator_id = (request.args.get('creator_id') or draft.get('creator_id') or 'everydaywithsteph').strip()
    products = draft.get('product_snapshot') or collection.get('items', []) or []
    rctx = _editor_retailer_context(collection)
    return render_template(
        'walmart_collection_create_post.html',
        collection=collection,
        products=products,
        product_count=len(products),
        creator_id=creator_id,
        default_public_slug=draft.get('public_slug') or public_slug,
        demo_auth_allowed=_walmart_content_demo_allowed(),
        existing_draft=draft,
        editor_mode='edit',
        shop_subdomain=SHOP_SUBDOMAIN,
        retailer_ctx=rctx,
    )


# Canonical retailer-agnostic routes.
@app.route('/collections/<collection_slug>/create-post')
def collection_create_post(collection_slug):
    """Canonical creator-voice editor for a trend collection (Walmart, Amazon, mixed)."""
    guard = _require_admin_page()
    if guard:
        return guard
    return _render_collection_create_post(collection_slug)


@app.route('/collections/<public_slug>/edit')
def collection_page_edit(public_slug):
    """Canonical editor for a published collection page."""
    guard = _require_admin_page()
    if guard:
        return guard
    return _render_collection_page_edit(public_slug)


# Backward-compatible aliases — old links keep working.
@app.route('/walmart/collections/<collection_slug>/create-post')
def walmart_collection_create_post(collection_slug):
    """Legacy alias → /collections/<slug>/create-post (kept so old links don't 404)."""
    qs = request.query_string.decode('utf-8') if request.query_string else ''
    target = f"/collections/{collection_slug}/create-post"
    if qs:
        target = f"{target}?{qs}"
    return redirect(target, code=302)


@app.route('/walmart/pages/<public_slug>/edit')
def walmart_page_edit(public_slug):
    """Legacy alias → /collections/<slug>/edit."""
    qs = request.query_string.decode('utf-8') if request.query_string else ''
    target = f"/collections/{public_slug}/edit"
    if qs:
        target = f"{target}?{qs}"
    return redirect(target, code=302)


@app.route('/api/walmart/collections/<collection_slug>/transcribe-voice', methods=['POST'])
def walmart_collection_transcribe_voice(collection_slug):
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    import collection_content as cc

    draft_id = request.form.get('draft_id')
    source_collection_slug, collection, _draft = cc.resolve_editor_collection(collection_slug, draft_id)
    if not collection:
        return jsonify({'error': 'Walmart collection not found'}), 404
    if 'audio' not in request.files:
        return jsonify({'error': 'audio file is required'}), 400
    audio = request.files['audio']
    allowed = {'audio/webm', 'video/webm', 'audio/ogg', 'audio/wav', 'audio/mpeg', 'audio/mp4', 'audio/x-m4a'}
    mimetype = (audio.mimetype or '').lower()
    if mimetype not in allowed:
        return jsonify({'error': f'Unsupported audio type: {mimetype or "unknown"}'}), 400
    audio.stream.seek(0, os.SEEK_END)
    size = audio.stream.tell()
    audio.stream.seek(0)
    if size <= 0:
        return jsonify({'error': 'audio file is empty'}), 400
    if size > 10 * 1024 * 1024:
        return jsonify({'error': 'audio file must be 10 MB or smaller'}), 400
    if not os.environ.get('OPENAI_API_KEY'):
        return jsonify({
            'error': 'OPENAI_API_KEY missing',
            'message': 'Voice transcription needs OPENAI_API_KEY. Paste notes still works.',
        }), 503

    suffix = {
        'audio/webm': '.webm', 'video/webm': '.webm', 'audio/ogg': '.ogg', 'audio/wav': '.wav',
        'audio/mpeg': '.mp3', 'audio/mp4': '.mp4', 'audio/x-m4a': '.m4a',
    }.get(mimetype, '.audio')
    tmp_path = ''
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            audio.save(tmp)
        with open(tmp_path, 'rb') as fh:
            resp = req.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f"Bearer {os.environ.get('OPENAI_API_KEY')}"},
                data={'model': 'whisper-1'},
                files={'file': (audio.filename or f'walmart-voice{suffix}', fh, mimetype)},
                timeout=60,
            )
        if resp.status_code >= 400:
            return jsonify({'error': 'transcription failed', 'message': resp.text[:500]}), 502
        data = resp.json()
        transcript = (data.get('text') or '').strip()
        if not transcript:
            return jsonify({'error': 'transcription returned no text'}), 502
        return jsonify({
            'transcript': transcript,
            'collection_slug': source_collection_slug,
            'message': 'Transcription complete',
        })
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@app.route('/api/walmart/collections/<collection_slug>/generate-post', methods=['POST'])
def walmart_collection_generate_post(collection_slug):
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    import collection_content as cc

    body = request.get_json(silent=True) or {}
    try:
        draft_id = body.get('draft_id')
        source_collection_slug, _draft = cc.resolve_source_collection_slug(collection_slug, draft_id)
        generated = cc.generate_walmart_collection_content(
            collection_slug=collection_slug,
            creator_id=(body.get('creator_id') or 'everydaywithsteph').strip(),
            voice_source_text=body.get('voice_source_text') or '',
            platform=body.get('platform') or 'facebook_group',
            tone=body.get('tone') or 'warm mom-to-mom',
            audience_context=body.get('audience_context') or 'busy moms looking for timely creator finds',
            allow_demo_fallback=False,
            regenerate_target=body.get('regenerate_target') or '',
            draft_id=draft_id,
        )
        response = {'source_type': cc.SOURCE_WALMART_TREND, 'source_collection_slug': source_collection_slug, **generated}
        if draft_id:
            response['draft_id'] = draft_id
        return jsonify(response)
    except cc.CollectionContentError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logging.exception('[WALMART_CONTENT] generate failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/api/walmart/collections/<collection_slug>/draft-page', methods=['POST'])
def walmart_collection_draft_page(collection_slug):
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    import collection_content as cc

    body = request.get_json(silent=True) or {}
    try:
        requested_status = (body.get('status') or 'draft').strip().lower()
        if requested_status not in {'draft', 'published', 'archived'}:
            requested_status = 'draft'
        draft = cc.save_walmart_collection_draft(collection_slug, body, status=requested_status)
        preview = cc.materialize_preview(int(draft['id']))
        return jsonify({
            'draft_id': draft['id'],
            'status': 'draft',
            'public_slug': preview['public_slug'],
            'preview_url': preview['preview_url'],
            'draft': draft,
        })
    except cc.CollectionContentError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logging.exception('[WALMART_CONTENT] draft failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/api/collection-content-drafts/<int:draft_id>/publish', methods=['POST'])
def collection_content_draft_publish(draft_id):
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    import collection_content as cc

    try:
        result = cc.publish_draft(draft_id)
        return jsonify(result)
    except cc.CollectionContentError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logging.exception('[WALMART_CONTENT] publish failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/api/collection-content-drafts/<int:draft_id>/unpublish', methods=['POST'])
def collection_content_draft_unpublish(draft_id):
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    import collection_content as cc

    try:
        result = cc.unpublish_draft(draft_id)
        return jsonify(result)
    except cc.CollectionContentError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logging.exception('[WALMART_CONTENT] unpublish failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/api/collection-content-drafts/<int:draft_id>/archive', methods=['POST'])
def collection_content_draft_archive(draft_id):
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    import collection_content as cc

    try:
        result = cc.archive_draft(draft_id)
        return jsonify(result)
    except cc.CollectionContentError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logging.exception('[WALMART_CONTENT] archive failed')
        return jsonify({'error': str(exc)}), 500


# ---------------------------------------------------------------------------
# Lightweight product controls + manual price refresh for the collection editor
# ---------------------------------------------------------------------------

def _load_draft_or_404(draft_id):
    import collection_content as cc
    try:
        did = int(draft_id)
    except (TypeError, ValueError):
        return None, (jsonify({'error': 'invalid draft_id'}), 400)
    draft = cc.get_draft(did)
    if not draft:
        return None, (jsonify({'error': 'draft not found'}), 404)
    return draft, None


def _save_draft_products(draft_id: int, products: list) -> None:
    """Replace product_snapshot_json on an existing draft. Bumps updated_at."""
    import collection_content as cc
    conn = cc._connect()
    try:
        conn.execute(
            "UPDATE collection_content_drafts SET product_snapshot_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(products), cc._now(), int(draft_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_collection_product_input(raw_value: str) -> tuple[str, str]:
    """Return (retailer, id) for Amazon ASIN/URL or Walmart SKU/URL."""
    value = str(raw_value or '').strip()
    if not value:
        return '', ''
    from utils.asin import extract_asin
    from walmart_trends import extract_walmart_sku_from_url

    walmart_sku = extract_walmart_sku_from_url(value)
    if walmart_sku:
        return 'walmart', walmart_sku
    asin = extract_asin(value)
    if not asin:
        raw_asin_match = re.fullmatch(r'[A-Z0-9]{10}', value.upper())
        url_asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', value, re.IGNORECASE)
        asin = (raw_asin_match.group(0) if raw_asin_match else '') or (
            url_asin_match.group(1).upper() if url_asin_match else ''
        )
    if asin:
        return 'amazon', asin
    if value.isdigit() and len(value) >= 5:
        return 'walmart', value
    return '', ''


def _build_amazon_snapshot_product(asin: str) -> dict:
    import amazon_trends

    store = amazon_trends.AmazonTrendStore()
    fallback_url = f"https://www.amazon.com/dp/{asin}"
    store.upsert_product(amazon_trends.AmazonTrendRecord(asin=asin, amazon_link=fallback_url))
    enricher = amazon_trends.AmazonProductEnricher(store)
    enriched = enricher.enrich(asin) or {}
    affiliate_url = store.affiliate_link_for(asin)
    if not affiliate_url:
        affiliate_url = amazon_trends._ensure_amazon_tag(
            enriched.get('detail_page_url') or enriched.get('amazon_link') or fallback_url
        )
        store.seed_workbook_affiliate_link(asin, affiliate_url)
    shop_url = amazon_trends.AmazonURLGeniusLinkService(store).ensure(affiliate_url, asin)
    return {
        'asin': asin,
        'product_name': enriched.get('product_title') or enriched.get('product_name') or enriched.get('title') or asin,
        'company_name': enriched.get('brand') or '',
        'brand': enriched.get('brand') or '',
        'price': enriched.get('price_display') or enriched.get('current_price') or '',
        'current_price': enriched.get('current_price') or '',
        'price_display': enriched.get('price_display') or '',
        'image_encoded_string': enriched.get('image_url') or '',
        'attribution_link': shop_url or affiliate_url,
        'retailer': 'Amazon',
        'retailer_name': 'Amazon',
        'network': 'amazon',
        'category': enriched.get('category') or '',
        'source_rank': None,
        'source_badges': [],
    }


def _build_walmart_snapshot_product(sku: str) -> dict:
    import walmart_trends

    store = walmart_trends.WalmartTrendStore()
    store.upsert_product_from_record(walmart_trends.TrendRecord(sku=sku))
    enriched = walmart_trends.WalmartProductEnricher(store).enrich(sku) or {}
    product_url = enriched.get('canonical_url') or enriched.get('url') or store.product_url_for_sku(sku)
    impact_url = walmart_trends.AffiliateLinkService(store).ensure(sku, product_url)
    shop_url = walmart_trends.URLGeniusLinkService(store).ensure(impact_url, sku)
    return {
        'asin': sku,
        'product_name': enriched.get('title') or enriched.get('product_title') or enriched.get('name') or f'Walmart find {sku}',
        'company_name': enriched.get('brand') or '',
        'brand': enriched.get('brand') or '',
        'price': enriched.get('price_display') or enriched.get('current_price') or '',
        'current_price': enriched.get('price_value') or enriched.get('current_price') or '',
        'price_display': enriched.get('price_display') or '',
        'image_encoded_string': enriched.get('image_url') or '',
        'attribution_link': shop_url or impact_url or product_url,
        'retailer': 'Walmart',
        'retailer_name': 'Walmart',
        'network': 'walmart',
        'category': enriched.get('category') or enriched.get('taxonomy') or '',
        'source_rank': None,
        'source_badges': [],
    }


@app.route('/api/walmart/collections/<collection_slug>/drafts/<draft_id>/products', methods=['POST'])
def walmart_draft_replace_products(collection_slug, draft_id):
    """Replace product list on a draft (used after reorder/remove)."""
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    draft, err = _load_draft_or_404(draft_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    products = body.get('products') or []
    if not isinstance(products, list):
        return jsonify({'error': 'products must be a list'}), 400
    _save_draft_products(int(draft['id']), products)
    return jsonify({'ok': True, 'count': len(products), 'products': products})


@app.route('/api/walmart/collections/<collection_slug>/drafts/<draft_id>/add-product', methods=['POST'])
def walmart_draft_add_product(collection_slug, draft_id):
    """Hydrate one Amazon or Walmart product and append to draft.product_snapshot."""
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    draft, err = _load_draft_or_404(draft_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    raw_product = body.get('product') or body.get('input') or body.get('asin') or body.get('sku') or ''
    retailer, product_id = _parse_collection_product_input(raw_product)
    if not product_id:
        return jsonify({'error': 'product is required', 'message': 'Enter an Amazon ASIN/URL or Walmart SKU/URL'}), 400

    existing = list(draft.get('product_snapshot') or [])
    if any(str(p.get('asin') or p.get('sku') or '').strip().upper() == product_id.upper() for p in existing if isinstance(p, dict)):
        return jsonify({'error': 'duplicate', 'message': f'{product_id} is already in this collection'}), 409

    try:
        if retailer == 'amazon':
            new_product = _build_amazon_snapshot_product(product_id.upper())
        elif retailer == 'walmart':
            new_product = _build_walmart_snapshot_product(product_id)
        else:
            return jsonify({'error': 'unsupported product input'}), 400
    except Exception as exc:
        logging.exception('[WALMART_CONTENT] add-product enrichment failed')
        return jsonify({'error': 'enrichment failed', 'message': str(exc)}), 502
    new_product['rank'] = len(existing) + 1
    existing.append(new_product)
    _save_draft_products(int(draft['id']), existing)
    return jsonify({'ok': True, 'count': len(existing), 'products': existing, 'added': new_product})


@app.route('/api/walmart/collections/<collection_slug>/drafts/<draft_id>/refresh-pricing', methods=['POST'])
def walmart_draft_refresh_pricing(collection_slug, draft_id):
    """Re-hydrate price/image for every product in the draft snapshot."""
    guard = _require_walmart_admin_if_configured()
    if guard:
        return guard
    draft, err = _load_draft_or_404(draft_id)
    if err:
        return err

    import walmart_storefront_enrichment as wse
    products = list(draft.get('product_snapshot') or [])
    updated = 0
    failed = 0
    new_list = []
    amazon_enricher = None
    for product in products:
        if not isinstance(product, dict):
            new_list.append(product)
            continue
        retailer = ''
        for field in ('retailer', 'network', 'retailer_name'):
            value = str(product.get(field) or '').strip().lower()
            if value in ('walmart', 'amazon'):
                retailer = value
                break
        try:
            if retailer == 'walmart':
                refreshed = wse.enrich_product_payload(product, fetch_live=True)
                new_list.append(refreshed)
                updated += 1
            elif retailer == 'amazon':
                if amazon_enricher is None:
                    import amazon_trends
                    amazon_enricher = amazon_trends.AmazonProductEnricher(amazon_trends.AmazonTrendStore())
                asin = str(product.get('asin') or product.get('sku') or '').strip()
                if not asin:
                    new_list.append(product)
                    failed += 1
                    continue
                data = amazon_enricher.enrich(asin) or {}
                merged = dict(product)
                if data.get('image_url'):
                    merged['image_encoded_string'] = data['image_url']
                if data.get('current_price') not in (None, ''):
                    merged['current_price'] = data['current_price']
                if data.get('price_display'):
                    merged['price_display'] = data['price_display']
                    merged['price'] = data['price_display']
                fresh_name = data.get('product_title') or data.get('product_name') or data.get('title')
                if fresh_name:
                    merged['product_name'] = merged.get('product_name') or fresh_name
                new_list.append(merged)
                updated += 1
            else:
                new_list.append(product)
        except Exception as exc:
            logging.warning('[WALMART_CONTENT] refresh-pricing failed for one product: %s', exc)
            new_list.append(product)
            failed += 1

    _save_draft_products(int(draft['id']), new_list)
    from datetime import datetime as _dt
    return jsonify({
        'updated': updated,
        'failed': failed,
        'last_checked_at': _dt.utcnow().replace(microsecond=0).isoformat(sep=' '),
        'products': new_list,
    })


@app.route('/admin/walmart-trends/workbooks', methods=['GET'])
def admin_walmart_trends_workbooks():
    """Return discovered workbook files from attached_assets/, newest first."""
    guard = _require_walmart_trends_admin()
    if guard:
        return guard
    from walmart_trends import discover_workbooks
    return jsonify(discover_workbooks())


def _validate_workbook_path(raw: str) -> tuple[str | None, str | None]:
    """Return (resolved_path_str, error_msg). Rejects paths outside attached_assets/."""
    from pathlib import Path as _Path
    try:
        candidate = _Path(raw).resolve()
        assets_root = _Path("attached_assets").resolve()
        if not str(candidate).startswith(str(assets_root) + os.sep) and candidate != assets_root:
            return None, "Workbook path must be inside attached_assets/"
        if candidate.suffix.lower() != ".xlsx":
            return None, "Workbook must be an .xlsx file"
        if not candidate.exists():
            return None, f"Workbook not found: {_Path(raw).name}"
    except Exception:
        return None, "Invalid workbook path"
    return str(candidate), None


@app.route('/admin/walmart-trends/bootstrap', methods=['POST'])
def admin_walmart_trends_bootstrap():
    """Seed product trends from a workbook in attached_assets/.

    Supports both Walmart_*.xlsx and Amazon_*.xlsx workbooks.
    Routes to the appropriate ingestion service based on filename prefix.

    JSON body: {"workbook": "attached_assets/Walmart_May12_Analysis.xlsx"}
    Omit 'workbook' to use the newest discovered workbook.
    """
    guard = _require_walmart_trends_admin()
    if guard:
        return guard
    _ensure_schema_ready()
    from walmart_trends import (
        DEFAULT_WORKBOOK, RefreshAlreadyRunning, WalmartTrendRefreshService,
        discover_workbooks, parse_workbook_filename,
    )

    body = request.get_json(silent=True) or {}
    raw_path = body.get('workbook')
    if not raw_path:
        workbooks = discover_workbooks()
        if not workbooks:
            return jsonify({'error': 'No workbook files found in attached_assets/'}), 400
        raw_path = workbooks[0]['path']

    workbook, err = _validate_workbook_path(raw_path)
    if err:
        return jsonify({'error': err}), 400

    file_meta = parse_workbook_filename(workbook)
    source = file_meta.get("source", "unknown")

    try:
        if source == "Amazon":
            from amazon_trends import AmazonTrendRefreshService
            result = AmazonTrendRefreshService().bootstrap_from_workbook(workbook)
        else:
            result = WalmartTrendRefreshService().bootstrap_from_workbook(workbook)
    except RefreshAlreadyRunning as exc:
        return jsonify({'status': 'locked', 'error': str(exc)}), 409
    status_code = 200 if result.status in {'success', 'partial'} else 500
    return jsonify({
        'run_id': result.run_id,
        'status': result.status,
        'source': source,
        'counts': result.counts,
        'failures': result.failures,
    }), status_code


@app.route('/admin/amazon-trends/enrich', methods=['POST'])
def admin_amazon_trends_enrich():
    """Prioritized Amazon enrichment pass — decoupled from workbook import.

    Body params (all optional):
      limit (int, default 30) — max ASINs per run
      max_workers (int, default 4) — concurrency
    """
    guard = _require_walmart_trends_admin()
    if guard:
        return guard
    _ensure_schema_ready()
    from amazon_trends import AmazonTrendRefreshService

    body = request.get_json(silent=True) or {}
    try:
        limit = max(1, min(200, int(body.get('limit', 30))))
    except (TypeError, ValueError):
        limit = 30
    try:
        max_workers = max(1, min(16, int(body.get('max_workers', 4))))
    except (TypeError, ValueError):
        max_workers = 4

    counts = AmazonTrendRefreshService().enrich_pending(limit=limit, max_workers=max_workers)
    return jsonify({'status': 'ok', 'counts': counts}), 200


@app.route('/admin/walmart-trends/refresh', methods=['POST'])
def admin_walmart_trends_refresh():
    """Run the recurring 7-day Impact API Walmart trend refresh."""
    guard = _require_walmart_trends_admin()
    if guard:
        return guard
    from walmart_trends import RefreshAlreadyRunning, WalmartTrendRefreshService

    try:
        result = WalmartTrendRefreshService().refresh_from_impact()
    except RefreshAlreadyRunning as exc:
        return jsonify({'status': 'locked', 'error': str(exc)}), 409
    status_code = 200 if result.status in {'success', 'partial'} else 500
    return jsonify({
        'run_id': result.run_id,
        'status': result.status,
        'counts': result.counts,
        'failures': result.failures,
    }), status_code



@app.route('/admin/walmart-trends/links/<sku>', methods=['GET'])
def admin_walmart_trends_link_inspect(sku):
    """Inspect current stored Walmart Impact/URLGenius links for one SKU."""
    guard = _require_walmart_trends_admin()
    if guard:
        return guard
    from walmart_trends import WalmartLinkRegenerationService

    include_redirect = request.args.get('include_redirect') == '1'
    return jsonify(WalmartLinkRegenerationService().inspect_sku(sku, include_redirect=include_redirect))


@app.route('/admin/walmart-trends/links/<sku>/regenerate', methods=['POST'])
def admin_walmart_trends_link_regenerate(sku):
    """Regenerate stale Walmart Impact + URLGenius links for one SKU."""
    guard = _require_walmart_trends_admin()
    if guard:
        return guard
    from walmart_trends import WalmartLinkRegenerationService

    payload = request.get_json(silent=True) or {}
    force = request.args.get('force') == '1' or bool(payload.get('force'))
    include_redirect = request.args.get('include_redirect') == '1' or bool(payload.get('include_redirect'))
    return jsonify(WalmartLinkRegenerationService().regenerate_sku(sku, force=force, include_redirect=include_redirect))


@app.route('/admin/walmart-trends/links/regenerate-stale', methods=['POST'])
def admin_walmart_trends_links_regenerate_stale():
    """Regenerate all locally detected stale Walmart Impact + URLGenius links."""
    guard = _require_walmart_trends_admin()
    if guard:
        return guard
    from walmart_trends import WalmartLinkRegenerationService

    payload = request.get_json(silent=True) or {}
    raw_limit = request.args.get('limit') or payload.get('limit')
    limit = int(raw_limit) if raw_limit not in (None, '') else None
    include_redirect = request.args.get('include_redirect') == '1' or bool(payload.get('include_redirect'))
    return jsonify(WalmartLinkRegenerationService().regenerate_all_stale(limit=limit, include_redirect=include_redirect))


@app.route('/admin/walmart-trends/storefront/enrich', methods=['POST'])
def admin_walmart_storefront_enrich():
    """Refresh Walmart metadata embedded in public storefront records.

    This only updates display metadata in collages, collection-content drafts,
    and posts. Existing Walmart affiliate links are preserved exactly.
    """
    guard = _require_walmart_trends_admin()
    if guard:
        return guard

    import walmart_storefront_enrichment as enrichment

    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get('dry_run'))
    slug = (payload.get('slug') or request.args.get('slug') or '').strip()
    post_id = payload.get('post_id') or request.args.get('post_id')
    include_collages = payload.get('include_collages')
    include_posts = payload.get('include_posts')
    include_collages = True if include_collages is None else bool(include_collages)
    include_posts = True if include_posts is None else bool(include_posts)
    raw_limit = payload.get('limit') or request.args.get('limit')
    limit = max(1, min(int(raw_limit), 500)) if raw_limit not in (None, '') else 500

    result = {
        'dry_run': dry_run,
        'collages_checked': 0,
        'collages_changed': 0,
        'drafts_checked': 0,
        'drafts_changed': 0,
        'posts_checked': 0,
        'posts_changed': 0,
        'items_enriched': 0,
        'samples': [],
    }

    conn = db_schema._connect(timeout=30)
    try:
        if include_collages:
            collage_where = ["COALESCE(status, 'published') IN ('published', 'draft')"]
            collage_params = []
            if slug:
                collage_where.append("slug = ?")
                collage_params.append(slug)
            collage_rows = conn.execute(
                "SELECT slug, products_json FROM collages "
                f"WHERE {' AND '.join(collage_where)} "
                "ORDER BY created_at DESC LIMIT ?",
                [*collage_params, limit],
            ).fetchall()
            for row in collage_rows:
                products = json.loads(row['products_json'] or '[]')
                enriched, stats = enrichment.enrich_product_list(products, fetch_live=True)
                if stats['walmart'] == 0:
                    continue
                result['collages_checked'] += 1
                result['items_enriched'] += stats['changed']
                if json.dumps(enriched, sort_keys=True, default=str) != json.dumps(products, sort_keys=True, default=str):
                    result['collages_changed'] += 1
                    result['samples'].append({'type': 'collage', 'slug': row['slug'], 'changed': stats['changed']})
                    if not dry_run:
                        conn.execute(
                            "UPDATE collages SET products_json = ? WHERE slug = ?",
                            (json.dumps(enriched), row['slug']),
                        )

            draft_where = ["source_type = ?"]
            draft_params = ['walmart_trend']
            if slug:
                draft_where.append("(public_slug = ? OR source_collection_slug = ? OR published_collage_slug = ?)")
                draft_params.extend([slug, slug, slug])
            draft_rows = conn.execute(
                "SELECT id, public_slug, source_collection_slug, product_snapshot_json "
                "FROM collection_content_drafts "
                f"WHERE {' AND '.join(draft_where)} "
                "ORDER BY id DESC LIMIT ?",
                [*draft_params, limit],
            ).fetchall()
            for row in draft_rows:
                products = json.loads(row['product_snapshot_json'] or '[]')
                enriched, stats = enrichment.enrich_product_list(products, fetch_live=True)
                if stats['walmart'] == 0:
                    continue
                result['drafts_checked'] += 1
                result['items_enriched'] += stats['changed']
                if json.dumps(enriched, sort_keys=True, default=str) != json.dumps(products, sort_keys=True, default=str):
                    result['drafts_changed'] += 1
                    result['samples'].append({'type': 'draft', 'id': row['id'], 'public_slug': row['public_slug'], 'changed': stats['changed']})
                    if not dry_run:
                        conn.execute(
                            "UPDATE collection_content_drafts SET product_snapshot_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (json.dumps(enriched), row['id']),
                        )

        if include_posts:
            post_where = ["LOWER(COALESCE(network, '')) = 'walmart'"]
            post_params = []
            if post_id:
                post_where.append("id = ?")
                post_params.append(int(post_id))
            rows = conn.execute(
                "SELECT * FROM posts "
                f"WHERE {' AND '.join(post_where)} "
                "ORDER BY created_at DESC LIMIT ?",
                [*post_params, limit],
            ).fetchall()
            for row in rows:
                post = dict(row)
                updates = enrichment.post_update_fields(post, fetch_live=True)
                result['posts_checked'] += 1
                if updates:
                    result['posts_changed'] += 1
                    result['items_enriched'] += 1
                    result['samples'].append({'type': 'post', 'id': post['id'], 'asin': post.get('asin'), 'fields': sorted(updates)})
                    if not dry_run:
                        assignments = ', '.join(f"{field} = ?" for field in updates)
                        conn.execute(
                            f"UPDATE posts SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            [*updates.values(), int(post['id'])],
                        )

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return jsonify(result)

@app.route('/sitemap.xml')
def shop_sitemap():
    """Auto-generated sitemap of all published collections.

    Mounted at /sitemap.xml on both hosts; the shop subdomain serves the same
    list (since both resolve canonical URLs to https://SHOP_SUBDOMAIN/<slug>).
    """
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    rows = conn.execute(
        "SELECT slug, COALESCE(created_at, CURRENT_TIMESTAMP) as updated "
        "FROM collages "
        "WHERE COALESCE(status,'published') = 'published'"
    ).fetchall()
    conn.close()

    base = f'https://{SHOP_SUBDOMAIN}'
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    parts.append(f'  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>0.9</priority></url>')
    parts.append(f'  <url><loc>{base}/collections</loc><changefreq>daily</changefreq><priority>0.9</priority></url>')
    parts.append(f'  <url><loc>{base}/trends</loc><changefreq>daily</changefreq><priority>0.8</priority></url>')
    parts.append(f'  <url><loc>{base}/posts</loc><changefreq>daily</changefreq><priority>0.8</priority></url>')
    for row in rows:
        slug, updated = row['slug'], row['updated']
        lastmod = _fmt_date(updated)
        parts.append(
            f'  <url><loc>{base}/{slug}</loc>'
            f'<lastmod>{lastmod}</lastmod>'
            f'<changefreq>weekly</changefreq>'
            f'<priority>0.7</priority></url>'
        )
    parts.append('</urlset>')
    return Response('\n'.join(parts), mimetype='application/xml')


@app.route('/robots.txt')
def shop_robots():
    """Public robots.txt for the shop subdomain. Disallows admin paths."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Disallow: /archer/\n"
        "Disallow: /dashboard/\n"
        f"Sitemap: https://{SHOP_SUBDOMAIN}/sitemap.xml\n"
    )
    return Response(body, mimetype='text/plain')


# ── POSTS QUEUE (Branch 2B) ──────────────────────────────────────────────────
@app.route('/archer/posts', methods=['GET'])
@require_admin_api
def archer_posts_list():
    """List posts for the queue UI. Query: status, collection_slug, creator_id."""
    import posts as _posts
    rows = _posts.list_posts(
        creator_id=request.args.get('creator_id', 'everydaywithsteph'),
        status=request.args.get('status') or None,
        collection_slug=request.args.get('collection_slug') or None,
        limit=int(request.args.get('limit', 200)),
    )
    return jsonify({'posts': rows, 'stats': _posts.stats(
        request.args.get('creator_id', 'everydaywithsteph')
    )})


@app.route('/archer/posts/<int:post_id>', methods=['PATCH'])
@require_admin_api
def archer_post_update(post_id):
    """Update a single post's editable fields (copy, angle, status, UTMs, smart_link)."""
    import posts as _posts
    body = request.get_json() or {}
    saved = _posts.update_post(post_id, body)
    if not saved:
        return jsonify({'error': 'post not found'}), 404
    return jsonify({'post': saved})


@app.route('/archer/posts/<int:post_id>', methods=['DELETE'])
@require_admin_api
def archer_post_delete(post_id):
    """Hard delete. Use bulk_status with 'archived' for soft delete."""
    import posts as _posts
    if not _posts.delete_post(post_id):
        return jsonify({'error': 'post not found'}), 404
    return jsonify({'ok': True})


@app.route('/archer/posts/bulk', methods=['POST'])
@require_admin_api
def archer_posts_bulk():
    """Bulk-update status on many posts at once.

    Body: { "ids": [1,2,3], "status": "approved" | "posted" | "archived" }
    """
    import posts as _posts
    body = request.get_json() or {}
    ids = body.get('ids') or []
    status = (body.get('status') or '').strip()
    if not ids or status not in {'draft', 'approved', 'posted', 'archived'}:
        return jsonify({'error': 'ids and valid status required'}), 400
    n = _posts.bulk_set_status([int(i) for i in ids], status)
    return jsonify({'updated': n})


@app.route('/archer/posts/export.csv', methods=['GET'])
@require_admin_page
def archer_posts_export_csv():
    """Export posts queue as CSV: created_at | angle | asin | copy | smart_link | image_note | status."""
    import csv, io
    import posts as _posts
    rows = _posts.list_posts(
        creator_id=request.args.get('creator_id', 'everydaywithsteph'),
        status=request.args.get('status') or None,
        collection_slug=request.args.get('collection_slug') or None,
        limit=int(request.args.get('limit', 1000)),
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['created_at', 'angle', 'asin', 'product_name', 'copy',
                'smart_link', 'image_note', 'status', 'collection_slug'])
    for r in rows:
        w.writerow([
            r.get('created_at', ''), r.get('angle', ''), r.get('asin', ''),
            r.get('product_name', ''), r.get('copy', ''),
            r.get('smart_link', ''), r.get('image_note', ''),
            r.get('status', ''), r.get('collection_slug', '') or '',
        ])
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="posts_queue.csv"'},
    )


@app.route('/archer/posts/manage', methods=['GET'])
def archer_posts_manage_page():
    """Dedicated operations page for saved organic posts and collections."""
    guard = _require_admin_page()
    if guard:
        return guard
    _ensure_schema_ready()
    import collection_service
    import posts as _posts
    creator_id = request.args.get('creator_id', 'everydaywithsteph')
    rows = _posts.list_posts(creator_id=creator_id, limit=300)
    collages = collection_service.list_collages(status='all', limit=100)
    return render_template(
        'organic_posts_manage.html',
        posts=rows,
        collages=collages,
        creator_id=creator_id,
    )


@app.route('/archer/posts/<int:post_id>/edit', methods=['GET'])
def archer_post_edit_page(post_id):
    """Edit a single saved organic post without loading the build queue."""
    guard = _require_admin_page()
    if guard:
        return guard
    import posts as _posts
    post = _posts.get_post(post_id)
    if not post:
        return "Post not found", 404
    return render_template('organic_post_edit.html', post=post, amazon_tag=AMAZON_TAG)


# ── CAMPAIGN BUILDER v3 (Branch 3) ───────────────────────────────────────────
@app.route('/archer/campaigns')
@require_admin_page
def archer_campaigns_page():
    """Bulk Campaign Builder page — picks N targets, generates N packages."""
    return render_template('archer_campaigns.html')


@app.route('/archer/campaigns/list', methods=['GET'])
@require_admin_api
def archer_campaigns_list():
    """List persisted campaigns_v3 packages with optional filters."""
    from product_api import ArcherAPI
    creator_id = request.args.get('creator_id', 'everydaywithsteph')
    status_filter = request.args.get('status')
    limit = int(request.args.get('limit', 100))

    where = ["COALESCE(creator_id,'everydaywithsteph') = ?"]
    params: list = [creator_id]
    if status_filter:
        where.append('status = ?')
        params.append(status_filter)

    conn = ArcherAPI()._db_connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM campaigns_v3 WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC LIMIT {limit}",
        params,
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        for col in ('layers_json', 'package_json', 'defaults_overrides_json',
                    'meta_campaign_ids_json'):
            try:
                d[col] = json.loads(d.get(col) or 'null')
            except (json.JSONDecodeError, TypeError):
                d[col] = None
        out.append(d)
    return jsonify({'campaigns': out})


@app.route('/archer/campaigns/<int:campaign_id>', methods=['GET'])
@require_admin_api
def archer_campaign_get(campaign_id):
    from product_api import ArcherAPI
    conn = ArcherAPI()._db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM campaigns_v3 WHERE id = ?", (campaign_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'campaign not found'}), 404
    d = dict(row)
    for col in ('layers_json', 'package_json', 'defaults_overrides_json',
                'meta_campaign_ids_json'):
        try:
            d[col] = json.loads(d.get(col) or 'null')
        except (json.JSONDecodeError, TypeError):
            d[col] = None
    return jsonify({'campaign': d})


@app.route('/archer/campaigns/<int:campaign_id>', methods=['PATCH'])
@require_admin_api
def archer_campaign_update(campaign_id):
    """Update a draft package — package_json (full replace), status, asset_url, notes."""
    from product_api import ArcherAPI
    body = request.get_json() or {}
    allowed = {
        'package_json', 'asset_url', 'asset_type', 'status',
        'notes', 'destination_url', 'meta_campaign_ids_json',
        'defaults_overrides_json', 'layers_json',
    }
    sets = []
    vals: list = []
    for k, v in body.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(json.dumps(v) if k.endswith('_json') and not isinstance(v, str) else v)
    if not sets:
        return jsonify({'error': 'no editable fields supplied'}), 400
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if body.get('status') == 'exported':
        sets.append("exported_at = CURRENT_TIMESTAMP")
    if body.get('status') == 'built':
        sets.append("built_at = CURRENT_TIMESTAMP")
    vals.append(campaign_id)
    conn = ArcherAPI()._db_connect()
    conn.execute(
        f"UPDATE campaigns_v3 SET {', '.join(sets)} WHERE id = ?", vals,
    )
    conn.commit()
    conn.close()
    return archer_campaign_get(campaign_id)


@app.route('/archer/campaigns/<int:campaign_id>', methods=['DELETE'])
@require_admin_api
def archer_campaign_delete(campaign_id):
    from product_api import ArcherAPI
    conn = ArcherAPI()._db_connect()
    cur = conn.execute("DELETE FROM campaigns_v3 WHERE id = ?", (campaign_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({'error': 'campaign not found'}), 404
    return jsonify({'ok': True})


@app.route('/archer/campaigns/<int:campaign_id>/export', methods=['POST'])
@require_admin_api
def archer_campaign_export(campaign_id):
    """Mark a package exported and return the paste-ready Ryze MCP prompt."""
    import campaign_builder as cb
    from product_api import ArcherAPI

    conn = ArcherAPI()._db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM campaigns_v3 WHERE id = ?", (campaign_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'campaign not found'}), 404
    pkg = json.loads(row['package_json'] or '{}')
    creator = db_schema.get_creator(row['creator_id'])

    errors = cb.validate_package(pkg)
    if errors:
        conn.close()
        return jsonify({'error': 'package validation failed', 'errors': errors}), 400

    conn.execute(
        "UPDATE campaigns_v3 SET status = 'exported', "
        "exported_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?", (campaign_id,),
    )
    conn.commit()
    conn.close()

    return jsonify({
        'campaign_id': campaign_id,
        'package':     pkg,
        'ryze_prompt': cb.render_ryze_prompt(pkg, creator),
        'status':      'exported',
    })


@app.route('/archer/campaigns/generate', methods=['POST'])
@require_admin_api
def archer_campaigns_generate():
    """Bulk-generate campaign packages.

    Body:
    {
      "creator_id": "everydaywithsteph",
      "targets": [                       # one package per target
        {"kind": "asin", "value": "B0...", "product_name": "...",
         "brand": "...", "asset_url": "https://...", "asset_type": "static_image"},
        {"kind": "collection", "value": "summer-essentials", ...}
      ],
      "layer_ids": ["L1","L2","L3"],
      "defaults_override": {...},
      "utm_auto": true,
      "auto_generate_copy": true        # call Claude per-target for layer copies
    }
    """
    import campaign_builder as cb
    from product_api import ArcherAPI

    body = request.get_json() or {}
    creator_id = (body.get('creator_id') or 'everydaywithsteph').strip()
    targets = body.get('targets') or []
    layer_ids = body.get('layer_ids') or ['L1']
    utm_auto = bool(body.get('utm_auto', True))
    auto_generate_copy = bool(body.get('auto_generate_copy', True))
    defaults_override = body.get('defaults_override') or {}

    if not targets:
        return jsonify({'error': 'targets is required'}), 400
    if not layer_ids:
        return jsonify({'error': 'layer_ids is required'}), 400

    creator = db_schema.get_creator(creator_id)
    if not (creator.get('fb_page_id') or '').strip():
        creator['fb_page_id'] = db_schema.DEFAULT_CREATOR.get('fb_page_id', '')

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

    persisted = []
    errors_out = []
    for target in targets:
        try:
            asset_url = target.get('asset_url', '').strip()
            asset_type = target.get('asset_type', 'static_image')
            if not asset_url:
                # Fallback: derive from ASIN image CDN for ASIN targets
                if target.get('kind') == 'asin' and target.get('value'):
                    asset_url = (
                        f"https://ws-na.amazon-adsystem.com/widgets/q?"
                        f"_encoding=UTF8&ASIN={target['value']}&Format=_SL250_"
                    )

            # Generate per-layer copy via Claude (or use caller-supplied)
            layer_copies = target.get('layer_copies')
            if not layer_copies and auto_generate_copy:
                layer_copies = _generate_layer_copies(
                    client, creator_id, target, layer_ids,
                )
            if not layer_copies:
                # Last-resort placeholder so the package still validates
                layer_copies = [
                    {'layer_id': lid, 'headline': (target.get('product_name') or 'Shop Now')[:38],
                     'body': 'Edit this copy in the campaign card.',
                     'description': '', 'cta': 'SHOP_NOW'}
                    for lid in layer_ids
                ]

            pkg = cb.build_new_campaign_package(
                creator=creator, target=target,
                selected_layer_ids=layer_ids,
                asset_url=asset_url, asset_type=asset_type,
                layer_copies=layer_copies,
                destination_url=target.get('destination_url'),
                defaults_override=defaults_override,
                utm_auto=utm_auto,
            )

            # Persist
            conn = ArcherAPI()._db_connect()
            cur = conn.execute(
                """INSERT INTO campaigns_v3
                   (creator_id, package_type, target_type, target_value,
                    brand_slug, product_slug, product_name, destination_url,
                    layers_json, asset_url, asset_type, package_json,
                    defaults_overrides_json, utm_auto, status)
                   VALUES (?, 'new_campaign', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft') RETURNING id""",
                (
                    creator_id,
                    target.get('kind', 'asin'),
                    target.get('value', ''),
                    pkg['brand'],
                    pkg['product_slug'],
                    pkg.get('product') or '',
                    pkg['destination_url'],
                    json.dumps(layer_ids),
                    asset_url,
                    asset_type,
                    json.dumps(pkg),
                    json.dumps(defaults_override),
                    1 if utm_auto else 0,
                ),
            )
            conn.commit()
            campaign_id = db_schema._last_id(cur)
            conn.close()

            persisted.append({
                'id': campaign_id,
                'target': target,
                'package': pkg,
                'validation_errors': cb.validate_package(pkg),
            })
        except Exception as e:
            logging.exception('[CAMPAIGNS] generation failed for target')
            errors_out.append({'target': target, 'error': str(e)})

    return jsonify({
        'created':       len(persisted),
        'campaigns':     persisted,
        'errors':        errors_out,
    })


def _generate_layer_copies(client, creator_id, target, layer_ids):
    """Call Claude to generate layer-specific copy for one target."""
    from prompts import build_layer_copy_prompt
    system = build_layer_copy_prompt(creator_id)

    target_kind = target.get('kind', 'asin')
    name = target.get('product_name') or target.get('value') or 'item'
    brand = target.get('brand') or ''
    price = target.get('price') or ''
    target_label = (
        f"Collection landing page: {name}"
        if target_kind == 'collection'
        else f"Product: {name}{' by ' + brand if brand else ''}{' · ' + price if price else ''}"
    )
    user_msg = (
        f"{target_label}\n"
        f"Generate copy for these layers: {', '.join(layer_ids)}\n"
        f"Return JSON with one object per layer."
    )
    msg = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=2500, system=system,
        messages=[{'role': 'user', 'content': user_msg}],
    )
    raw = msg.content[0].text.strip().replace('```json', '').replace('```', '').strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            raise
        parsed = json.loads(m.group(0))
    return parsed.get('layer_copies', [])


@app.route('/archer/campaigns/boost', methods=['POST'])
@require_admin_api
def archer_campaigns_boost():
    """Build and persist a boost_post package.

    Body: { "creator_id": "...", "post_id": <internal posts.id>,
            "meta_post_id": "847392847362948", "boost_overrides": {...} }
    """
    import campaign_builder as cb
    import posts as _posts
    from product_api import ArcherAPI

    body = request.get_json() or {}
    creator_id = (body.get('creator_id') or 'everydaywithsteph').strip()
    post_id = body.get('post_id')
    meta_post_id = (body.get('meta_post_id') or '').strip()
    boost_overrides = body.get('boost_overrides') or {}

    if not meta_post_id:
        return jsonify({'error': 'meta_post_id is required (paste from Meta Business Suite)'}), 400

    creator = db_schema.get_creator(creator_id)
    if not (creator.get('fb_page_id') or '').strip():
        creator['fb_page_id'] = db_schema.DEFAULT_CREATOR.get('fb_page_id', '')
    product_slug = 'post'
    brand_slug = None
    target_value = meta_post_id

    if post_id:
        p = _posts.get_post(int(post_id))
        if p:
            product_slug = (p.get('angle') or '')[:30] or 'post'
            target_value = f"{post_id}:{meta_post_id}"
            if p.get('product_brand'):
                brand_slug = (p['product_brand'] or '').lower().replace(' ', '')[:20]

    pkg = cb.build_boost_post_package(
        creator=creator, meta_post_id=meta_post_id,
        boost_overrides=boost_overrides,
        product_slug=product_slug,
        brand_slug=brand_slug,
        utm_auto=bool(body.get('utm_auto', True)),
    )

    conn = ArcherAPI()._db_connect()
    cur = conn.execute(
        """INSERT INTO campaigns_v3
           (creator_id, package_type, target_type, target_value,
            brand_slug, product_slug, package_json, meta_post_id, status, utm_auto)
           VALUES (?, 'boost_post', 'post', ?, ?, ?, ?, ?, 'draft', 1) RETURNING id""",
        (
            creator_id, target_value, brand_slug, product_slug,
            json.dumps(pkg), meta_post_id,
        ),
    )
    conn.commit()
    campaign_id = db_schema._last_id(cur)
    conn.close()

    return jsonify({
        'id':       campaign_id,
        'package':  pkg,
        'validation_errors': cb.validate_package(pkg),
    })


@app.route('/archer/track_click', methods=['POST'])
def archer_track_click():
    from product_api import ArcherAPI
    data = request.get_json() or {}
    a = ArcherAPI()
    conn = a._db_connect()
    conn.execute(
        "INSERT INTO click_log (asin, slug, fbclid, attribution_url) VALUES (?,?,?,?)",
        (data.get('asin'), data.get('slug'), data.get('fbclid'), data.get('attribution_url'))
    )
    conn.execute(
        "UPDATE collages SET click_count = click_count + 1 WHERE slug=?",
        (data.get('slug'),)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/archer/campaigns/fetch-product', methods=['POST'])
@require_admin_api
def archer_fetch_product():
    """Fetch product details for a single ASIN (via Crawlbase) or Walmart SKU (via Walmart API).

    Body: {"rawId": "B0CLRNS4DD", "platform": "amazon", "url": "https://..."}
    Returns: product object with name, price, imageUrl, status
    """
    from product_api import WalmartAPI
    from product_lookup_service import resolve_amazon_product

    body = request.get_json() or {}
    raw_id  = (body.get('rawId') or '').strip()
    platform = (body.get('platform') or '').strip()
    url      = (body.get('url') or '').strip()

    if not raw_id or not platform:
        return jsonify({'error': 'rawId and platform are required'}), 400

    base = {'rawId': raw_id, 'platform': platform, 'url': url}

    try:
        if platform == 'amazon':
            data = resolve_amazon_product(raw_id)
            if data and (data.get('product_name') or data.get('price')):
                return jsonify({**base, 'name': data.get('product_name', ''),
                                'price': (data.get('price') or '').replace('$', '').strip(),
                                'imageUrl': data.get('image_encoded_string', ''),
                                'description': '', 'status': 'fetched'})
            return jsonify({**base, 'status': 'manual'})

        if platform == 'walmart':
            walmart = WalmartAPI()
            data = walmart.get_item_by_id(raw_id)
            if data and data.get('name'):
                return jsonify({**base, 'name': data.get('name', ''),
                                'price': data.get('price', ''),
                                'imageUrl': data.get('imageUrl', ''),
                                'description': '', 'status': 'fetched'})
            return jsonify({**base, 'status': 'manual'})

        return jsonify({**base, 'status': 'error', 'error': 'Unknown platform'}), 400

    except Exception as e:
        logging.exception('[FETCH-PRODUCT] failed')
        return jsonify({**base, 'status': 'error', 'error': str(e)}), 500


@app.route('/archer/image_proxy')
@require_admin_api
def archer_image_proxy():
    """Proxy an image URL so the browser can download it without CORS issues."""
    url = request.args.get('url', '').strip()
    filename = request.args.get('filename', 'product.jpg')
    if not url or not url.startswith('http'):
        return jsonify({'error': 'invalid url'}), 400
    try:
        r = req.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        content_type = r.headers.get('Content-Type', 'image/jpeg')
        return Response(
            r.content,
            headers={
                'Content-Type': content_type,
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# /archer/ads, /archer/organic, /archer/products (HTML page) removed
# in the Shop-MomandMe strip-down (2026-05-17) along with their templates.
# /archer/posts/<id>/edit (KEEP) is the canonical organic-post editor;
# /archer/organic's redirect-to-edit behavior is no longer needed because
# the editor links direct now.


# ── INSIGHTS: clicks × earnings × paid attribution ──────────────────────────
@app.route('/insights')
@require_admin_page
def insights_page():
    """Insights dashboard. Query params:
      window:       today | yesterday | 7d | 30d | custom (default: 30d)
      start, end:   ISO dates when window=custom
      creator_id:   defaults to 'everydaywithsteph'
      tab:          collections | posts | ads (default: collections)
    """
    import insights as _ins
    creator_id = (request.args.get('creator_id') or 'everydaywithsteph').strip()
    window = (request.args.get('window') or '30d').strip()
    custom_start = request.args.get('start')
    custom_end = request.args.get('end')
    tab = (request.args.get('tab') or 'collections').strip()

    start, end, label = _ins.resolve_window(window, custom_start, custom_end)

    overview = _ins.overview(creator_id, start, end)
    collections = _ins.collections_summary(creator_id, start, end)
    posts = _ins.posts_summary(creator_id, start, end)
    ads = _ins.ads_summary(creator_id, start, end, pull_archer_now=(tab == 'ads'))

    return render_template('insights.html',
        creator_id=creator_id,
        creators=db_schema.list_creators(),
        window=window, window_label=label,
        start=start, end=end,
        tab=tab,
        overview=overview,
        collections=collections,
        posts=posts,
        ads=ads,
    )


# ── ONE-TIME: seed production database from dev export ───────────────────────
@app.route('/admin/seed-production', methods=['POST'])
def seed_production():
    """
    One-time endpoint: runs scripts/prod_seed.sql against the current DATABASE_URL.
    Safe to call multiple times — INSERT … ON CONFLICT DO NOTHING prevents duplicates.

    Auth: server session (set via /admin/login) OR X-Walmart-Trends-Admin-Token
    header / Authorization: Bearer <token>. Same posture as other admin APIs.
    Previously protected by a hardcoded ?token=SEED_MMC_2026 URL parameter,
    which leaked through logs/history — removed in audit follow-up 0.2.
    """
    guard = _require_walmart_trends_admin()
    if guard:
        return guard

    import os as _os
    sql_path = _os.path.join(_os.path.dirname(__file__), 'scripts', 'prod_seed.sql')
    if not _os.path.exists(sql_path):
        return jsonify({'error': 'seed file not found'}), 404

    import psycopg2 as _psycopg2
    import os as _os2

    DATABASE_URL = _os2.environ.get('DATABASE_URL', '')
    if not DATABASE_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500

    raw_conn = _psycopg2.connect(DATABASE_URL)
    raw_conn.autocommit = False
    ok = 0
    skipped = 0
    errors = []
    try:
        with open(sql_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        cur = raw_conn.cursor()
        for line in lines:
            line = line.strip()
            # Only process INSERT statements; skip comments, SET, psql meta-commands
            if not line or line.startswith('--') or line.startswith('\\') or line.upper().startswith('SET '):
                continue
            if not line.upper().startswith('INSERT INTO'):
                continue

            # Strip trailing semicolon for re-adding with ON CONFLICT
            stmt = line.rstrip(';').strip()
            # Strip schema prefix so it works with the default search_path
            stmt = stmt.replace('INSERT INTO public.', 'INSERT INTO ')
            # Add ON CONFLICT DO NOTHING to make this idempotent
            if 'ON CONFLICT' not in stmt.upper():
                stmt += ' ON CONFLICT DO NOTHING'

            try:
                cur.execute("SAVEPOINT sp")
                cur.execute(stmt)
                cur.execute("RELEASE SAVEPOINT sp")
                ok += 1
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp")
                cur.execute("RELEASE SAVEPOINT sp")
                skipped += 1
                if len(errors) < 5:
                    errors.append(str(e)[:140])

        raw_conn.commit()

        # Reset sequences so next inserts don't collide with seeded IDs
        serial_tables = [
            'posts', 'earnings_amazon', 'attribution_paid', 'storefront_chat_sessions',
            'campaigns_v3', 'click_log', 'collection_content_drafts', 'walmart_refresh_runs',
            'walmart_product_performance_snapshots', 'walmart_affiliate_links',
            'walmart_urlgenius_links', 'walmart_collection_items',
            'amazon_product_performance_snapshots', 'amazon_affiliate_links',
        ]
        for tbl in serial_tables:
            try:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{tbl}','id'),COALESCE(MAX(id),1),true)"
                    f' FROM "{tbl}"'
                )
                raw_conn.commit()
            except Exception:
                raw_conn.rollback()
    finally:
        raw_conn.close()

    return jsonify({
        'status': 'done',
        'inserted': ok,
        'skipped': skipped,
        'sample_errors': errors[:5],
    })


# ── ADMIN: creator management ────────────────────────────────────────────────
@app.route('/admin/creators', methods=['GET'])
def admin_creators():
    """List creators + edit/create form. Gated by admin session."""
    guard = _require_admin_page()
    if guard:
        return guard
    creators = db_schema.list_creators()
    return render_template('admin_creators.html', creators=creators)


@app.route('/admin/creators', methods=['POST'])
@require_admin_api
def admin_creators_save():
    """Create or update a creator from the admin form."""
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    creator_id = (body.get('id') or '').strip().lower()
    if not creator_id:
        return jsonify({'error': 'id is required (lowercase slug)'}), 400
    payload = {
        'id':                 creator_id,
        'display_name':       (body.get('display_name') or '').strip(),
        'handle':             (body.get('handle') or '').strip(),
        'brand_label':        (body.get('brand_label') or '').strip(),
        'fb_pixel_id':        (body.get('fb_pixel_id') or '').strip(),
        'amazon_tag':         (body.get('amazon_tag') or '').strip(),
        'meta_ad_account_id': (body.get('meta_ad_account_id') or '').strip(),
        'ltk_url':            (body.get('ltk_url') or '').strip(),
        'facebook_url':       (body.get('facebook_url') or '').strip(),
        'voice_prompt':       (body.get('voice_prompt') or '').strip(),
        'theme_default':      (body.get('theme_default') or 'coral').strip(),
    }
    if not payload['display_name']:
        return jsonify({'error': 'display_name is required'}), 400
    saved = db_schema.upsert_creator(payload)
    return jsonify({'creator': saved})


@app.route('/admin/creators/<creator_id>', methods=['GET'])
@require_admin_api
def admin_creator_get(creator_id):
    creator = db_schema.get_creator(creator_id)
    return jsonify({'creator': creator})

# /archer/generate_ad_copy, /archer/ads/save, /archer/ads/campaigns removed
# in the Shop-MomandMe strip-down (2026-05-17). Used only by
# templates/archer_ads.html and templates/dashboard.html (both deleted).
# The campaigns table itself stays in the local SQLite cache — it's read by
# /archer/campaigns/list which is being removed in cluster 4, after which
# the orphan table can be dropped in a future schema-cleanup pass.

# ── URLGENIUS ─────────────────────────────────────────────────────────────────

AMAZON_TAG = os.environ.get('AMAZON_AFFILIATE_TAG', 'mommymedeals-20')

# Valid source × medium combinations
# Includes both legacy values and new UTM schema (April 2026)
VALID_PLACEMENTS = {
    'fb-group':  ['organic'],
    'fb-ad':     ['dark', 'boost'],
    # Legacy
    'facebook':  ['organic', 'paid', 'organic_social', 'paid_social', 'boosted_post'],
    'instagram': ['organic', 'paid', 'organic_social', 'organic_video', 'paid_social'],
    'tiktok':    ['organic', 'paid', 'organic_video', 'organic_social', 'paid_social'],
    'email':     ['newsletter', 'email'],
    'linkinbio': ['organic_social'],
    'steph-ai':  ['ai-agent'],
}

# utm_content: caller may supply override; otherwise auto-derived from affiliate network
NETWORK_CONTENT = {
    'amazon':  'amazon-assoc',
    'archer':  'archer',
    'levanta': 'levanta',
}

# URLGenius sync is now user-triggered only via /urlgenius/sync.
# Intentionally no startup seed to keep boot path fast and deterministic.


def _slug_part(value: str, max_len: int = 15) -> str:
    slug = re.sub(r'[^a-z0-9]+', '', (value or '').lower())
    return slug[:max_len]


def _organic_campaign_for_product(product: dict, asin: str) -> str:
    brand_raw = product.get('company_name') or product.get('brand') or ''
    brand = _slug_part((brand_raw.split() or ['brand'])[0], 10) or 'brand'
    name_raw = product.get('product_name') or product.get('name') or asin
    brand_l = brand_raw.lower()
    name_words = [
        w for w in re.split(r'\s+', name_raw.lower())
        if w and w not in brand_l
    ]
    prod = _slug_part(' '.join(name_words), 12) or _slug_part(asin, 12) or 'product'
    return f"{brand}_{prod}_organic"


def _organic_static_utm(product: dict, asin: str, angle: str, defaults: dict | None = None) -> dict:
    defaults = defaults or {}
    angle_slug = re.sub(r'[^a-z0-9-]+', '-', (angle or 'organic').lower()).strip('-') or 'organic'
    return {
        'source': defaults.get('source') or 'facebook',
        'medium': defaults.get('medium') or 'organic_social',
        'campaign': defaults.get('campaign') or _organic_campaign_for_product(product, asin),
        'content': defaults.get('content') or f"organic_{angle_slug}_static",
        'term': defaults.get('term') or '',
    }


def _extract_urlgenius_link_id(link_obj: dict) -> str:
    if not isinstance(link_obj, dict):
        return ''
    return link_obj.get('id') or link_obj.get('link_id') or ''


def _amazon_urlgenius_link(asin: str, utm: dict, force_new: bool = False) -> dict:
    """Build an Amazon affiliate URL and wrap/store it in URLGenius when configured."""
    from product_api import URLGeniusAPI

    affiliate_url = f"https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}"
    utm_source = (utm.get('source') or '').strip().lower()
    utm_medium = (utm.get('medium') or '').strip().lower()
    utm_campaign = (utm.get('campaign') or '').strip()
    utm_content = (utm.get('content') or '').strip() or NETWORK_CONTENT['amazon']
    utm_term = (utm.get('term') or '').strip()
    link_label = f"{utm_source}_{utm_medium}_{utm_campaign}_{__import__('datetime').datetime.now().strftime('%m%d')}"
    final_url = URLGeniusAPI._append_utms(
        affiliate_url,
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
        utm_content=utm_content,
        utm_term=utm_term or None,
    )

    ug = URLGeniusAPI()
    if not ug.api_key:
        return {
            'genius_url': affiliate_url,
            'affiliate_url': affiliate_url,
            'final_url': final_url,
            'network': 'amazon',
            'label': link_label,
            'utm': {
                'utm_source': utm_source,
                'utm_medium': utm_medium,
                'utm_campaign': utm_campaign,
                'utm_content': utm_content,
                'utm_term': utm_term,
            },
            'urlgenius': False,
            'link_id': '',
        }

    ug_result = ug.create_link(
        destination_url=affiliate_url,
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
        utm_content=utm_content,
        utm_term=utm_term or None,
        force_new=force_new,
    )
    link_obj = ug_result.get('link', {}) if isinstance(ug_result, dict) else {}
    genius_url = (
        link_obj.get('genius_url')
        if isinstance(link_obj, dict)
        else None
    ) or affiliate_url
    return {
        'genius_url': genius_url,
        'affiliate_url': affiliate_url,
        'final_url': (link_obj.get('final_url') if isinstance(link_obj, dict) else '') or final_url,
        'network': 'amazon',
        'label': link_label,
        'utm': {
            'utm_source': utm_source,
            'utm_medium': utm_medium,
            'utm_campaign': utm_campaign,
            'utm_content': utm_content,
            'utm_term': utm_term,
        },
        'urlgenius': True,
        'from_registry': ug_result.get('_from_registry', False) if isinstance(ug_result, dict) else False,
        'link_id': _extract_urlgenius_link_id(link_obj),
    }


def _make_smart_link(asin: str, network: str = 'amazon', utm_source: str = 'fb-group',
                     utm_medium: str = 'organic', utm_campaign: str = '',
                     utm_term: str = '', creator_id: str = 'everydaywithsteph') -> dict:
    """
    Internal helper: build an affiliate URL and wrap it in URLGenius.

    As of Phase 2A this delegates to link_builder.build_smart_link() which
    routes through the LinkBuilder registry — Archer/URLGenius today, Walmart
    Impact in Phase 2C. Signature preserved so existing call sites stay valid.
    """
    from link_builder import build_smart_link as _build

    # Map legacy 'network' values to registry keys. 'levanta' had no real
    # generator; treat it as a passthrough Amazon link with the Levanta
    # utm_content fallback.
    registry_network = 'archer' if network in ('amazon', 'archer') else network

    utm_content = NETWORK_CONTENT.get(network, network)
    return _build(
        item_id=asin,
        network=registry_network,
        utm={
            'source':   utm_source,
            'medium':   utm_medium,
            'campaign': utm_campaign,
            'content':  utm_content,
            'term':     utm_term,
        },
        creator_id=creator_id,
    )


@app.route('/urlgenius/smart_link', methods=['POST'])
@require_admin_api
def urlgenius_smart_link():
    """
    Generate a URLGenius deep link for a product using the full UTM attribution schema.
    Body: {
      asin: str,
      network: 'amazon' | 'archer' | 'levanta',
      placement: { source, medium, campaign, content?, term? },
      force_new?: bool
    }
    utm_content: caller-supplied placement.content takes precedence; falls back to
    NETWORK_CONTENT auto-derive (e.g. 'amazon-assoc'). Use organic_[angle]_static
    or organic_[angle]_collection convention from UTM Schema Reference.
    Returns { genius_url, affiliate_url, network, label, utm }
    """
    from product_api import ArcherAPI, LevantaAPI, URLGeniusAPI
    body = request.get_json() or {}
    asin = body.get('asin', '').strip()
    network = body.get('network', 'amazon')
    force_new = bool(body.get('force_new', False))

    if not asin:
        return jsonify({'error': 'asin is required'}), 400

    # ── Validate placement ──────────────────────────────────────────────────
    placement = body.get('placement') or {}
    utm_source   = (placement.get('source') or '').strip().lower()
    utm_medium   = (placement.get('medium') or '').strip().lower()
    utm_campaign = (placement.get('campaign') or '').strip()
    utm_term     = (placement.get('term') or '').strip()

    if not utm_source or not utm_medium:
        return jsonify({'error': 'placement.source and placement.medium are required'}), 400
    if not utm_campaign:
        return jsonify({'error': 'placement.campaign is required'}), 400

    valid_mediums = VALID_PLACEMENTS.get(utm_source)
    if valid_mediums is None:
        return jsonify({'error': f'Invalid source "{utm_source}". Valid: {list(VALID_PLACEMENTS)}'}), 400
    if utm_medium not in valid_mediums:
        return jsonify({'error': f'Invalid medium "{utm_medium}" for source "{utm_source}". Valid: {valid_mediums}'}), 400

    # ── utm_content: caller override takes precedence, else auto-derive ────────
    utm_content = (placement.get('content') or '').strip() or NETWORK_CONTENT.get(network, network)

    # ── Build affiliate URL ─────────────────────────────────────────────────
    affiliate_url = None

    if network == 'amazon':
        try:
            result = _amazon_urlgenius_link(
                asin,
                {
                    'source': utm_source,
                    'medium': utm_medium,
                    'campaign': utm_campaign,
                    'content': utm_content,
                    'term': utm_term,
                },
                force_new=force_new,
            )
            return jsonify(result)
        except Exception as e:
            logging.error(f"[URLGENIUS] smart_link amazon failed: {e}")
            affiliate_url = f"https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}"

    elif network == 'archer':
        a = ArcherAPI()
        label = f"steph-archer-{asin.lower()}-{int(__import__('time').time())}"
        result = a.generate_link(asin, label=label)
        if not result:
            return jsonify({'error': 'Archer link generation failed'}), 500
        affiliate_url = (result.get('attribution_link') or result.get('url')
                         or result.get('link') or result.get('short_url'))
        if not affiliate_url:
            return jsonify({'error': 'Archer returned no URL', 'raw': result}), 500

    elif network == 'levanta':
        lv = LevantaAPI()
        try:
            result = lv.create_product_link(asin)
            affiliate_url = (result.get('url') or result.get('link')
                             or result.get('trackingUrl') or result.get('attribution_link'))
            if not affiliate_url:
                return jsonify({'error': 'Levanta returned no URL', 'raw': result}), 500
        except Exception as e:
            return jsonify({'error': f'Levanta link generation failed: {e}'}), 500

    else:
        return jsonify({'error': f'Unknown network: {network}'}), 400

    # ── Wrap in URLGenius ───────────────────────────────────────────────────
    from datetime import datetime as _dt
    _mmdd = _dt.now().strftime('%m%d')
    link_label = f"{utm_source}_{utm_medium}_{utm_campaign}_{_mmdd}"
    utm_meta = {
        'utm_source': utm_source,
        'utm_medium': utm_medium,
        'utm_campaign': utm_campaign,
        'utm_content': utm_content,
        'utm_term': utm_term,
    }

    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({
            'genius_url': affiliate_url,
            'affiliate_url': affiliate_url,
            'final_url': affiliate_url,
            'network': network,
            'label': link_label,
            'utm': utm_meta,
            'urlgenius': False,
        })
    try:
        ug_result = ug.create_link(
            destination_url=affiliate_url,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            utm_content=utm_content,
            utm_term=utm_term or None,
            force_new=force_new,
        )
        link_obj = ug_result.get('link', {})
        # registry hit returns link_obj directly as the stored dict
        if isinstance(link_obj, dict) and link_obj.get('genius_url'):
            genius_url = link_obj['genius_url']
        else:
            genius_url = link_obj.get('genius_url', affiliate_url) if isinstance(link_obj, dict) else affiliate_url
        return jsonify({
            'genius_url': genius_url,
            'affiliate_url': affiliate_url,
            'final_url': (link_obj.get('final_url') if isinstance(link_obj, dict) else '') or None,
            'network': network,
            'label': link_label,
            'utm': utm_meta,
            'urlgenius': True,
            'from_registry': ug_result.get('_from_registry', False),
            'link_id': _extract_urlgenius_link_id(link_obj),
        })
    except Exception as e:
        logging.error(f"[URLGENIUS] smart_link failed: {e}")
        return jsonify({
            'genius_url': affiliate_url,
            'affiliate_url': affiliate_url,
            'final_url': affiliate_url,
            'network': network,
            'label': link_label,
            'utm': utm_meta,
            'urlgenius': False,
        })


# ARCHIVED — see /archive/routes/




# /archer/discovery/top_clicked removed in the Shop-MomandMe strip-down
# (2026-05-17). Only caller was organic_posts.html (deleted). P2.6
# (Product Discovery) is the framework rebuild target — different shape,
# different data source.
# Legacy EchoTribe-internal URLgenius admin surfaces removed in the
# Shop-MomandMe strip-down (2026-05-17): /urlgenius/create_link,
# /urlgenius/sync, /urlgenius/links, /urlgenius (page), /archer/urlgenius.
# The URLGeniusAPI class in product_api.py remains — it's used by the
# Walmart/Amazon trends affiliate-link wrapping and by /urlgenius/smart_link
# (KEEP). Re-introduction is scoped to a future "Seller Connections"
# creator feature, not the current launch.


# Legacy EchoTribe-internal Levanta surfaces removed in the Shop-MomandMe
# strip-down (2026-05-17): /levanta/generate_link, /levanta/deals, and
# /webhooks/levanta. The LevantaAPI class in product_api.py remains because
# /archer/search (KEEP) and the urlgenius/smart_link fallback still reference
# it. Re-introduction is scoped to a future "Seller Connections" creator
# feature, not the current launch.

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
