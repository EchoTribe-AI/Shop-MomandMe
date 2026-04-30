import os
import re
import json
import logging
import sqlite3
import time
import requests as req
from flask import Flask, send_from_directory, request, jsonify, render_template, Response
from dotenv import load_dotenv
import anthropic

# ── Multi-creator schema migrations + Steph seed (idempotent) ────────────────
# CRITICAL: bootstrap MUST run before importing prompts. The legacy STEPH_*
# constants in prompts.py are PEP-562 lazy attrs that resolve via DB queries
# on first import — if the creators table doesn't exist yet, the prompts
# import crashes the whole app at boot. Bootstrap creates the tables first.
import db_schema
try:
    db_schema.bootstrap()
except Exception as _e:
    logging.warning(f"[BOOT] db_schema.bootstrap failed: {_e}")

from product_api import ProductResolver, detect_category
from prompts import (
    build_chat_prompt, build_chat_products,
    STEPH_CAPTION_PROMPT, STEPH_AD_COPY_PROMPT,
    STEPH_ORGANIC_POSTS_PROMPT, STEPH_CAMPAIGN_PACKAGE_PROMPT,
)

load_dotenv()  # loads .env locally; Replit Secrets override in production

app = Flask(__name__)

THEMES = {
    'coral':    {'bg': '#fff5f5', 'accent': '#ff6b6b', 'btn': '#e85d26', 'text': '#1a1a17'},
    'ocean':    {'bg': '#e8f4f8', 'accent': '#2e7dd4', 'btn': '#0a6b52', 'text': '#0f4a8a'},
    'lavender': {'bg': '#f5f0ff', 'accent': '#a78bfa', 'btn': '#ec4899', 'text': '#4c1d95'},
    'forest':   {'bg': '#f0f7f2', 'accent': '#27693a', 'btn': '#8a5510', 'text': '#1a2e1a'},
    'midnight': {'bg': '#1a1a17', 'accent': '#e8e5dc', 'btn': '#888780', 'text': '#e8e5dc'},
    'peach':    {'bg': '#fdf6f0', 'accent': '#e85d26', 'btn': '#8a5510', 'text': '#1a1a17'},
    'clean':    {'bg': '#ffffff', 'accent': '#1a1a17', 'btn': '#2e7dd4', 'text': '#1a1a17'},
    'bold':     {'bg': '#fff8f6', 'accent': '#e85d26', 'btn': '#a02828', 'text': '#1a1a17'},
}

PIXEL_ID = os.environ.get('FB_PIXEL_ID', '1559451780790812')

# ── shop.echotribe.ai subdomain ──────────────────────────────────────────────
# When DNS for shop.echotribe.ai points to this Flask app, requests like
# https://shop.echotribe.ai/summer-essentials are rewritten so the existing
# /shop/<slug> handler renders. Cleaner share URLs without a /shop/ prefix.
SHOP_SUBDOMAIN = os.environ.get('SHOP_SUBDOMAIN', 'shop.echotribe.ai').lower()


@app.before_request
def _route_shop_subdomain():
    """If host == shop.echotribe.ai, rewrite GET requests to public-only routes.

    Public surface on the shop subdomain:
      GET  /                      → shop_directory()         (creator-aware index)
      GET  /sitemap.xml           → shop_sitemap()
      GET  /robots.txt            → shop_robots()
      GET  /<slug>                → shop_landing(slug)
      POST /archer/track_click    → tracking endpoint (passthrough)
      *    /static/*              → passthrough for assets
    Anything else 404s on the public subdomain.
    """
    host = (request.host or '').split(':')[0].lower()
    if host != SHOP_SUBDOMAIN:
        return  # normal routing for the dashboard host

    path = request.path or '/'

    # Passthroughs the public surface itself needs
    if path == '/archer/track_click':
        return
    if path.startswith('/static/'):
        return

    if request.method == 'GET':
        if path == '/' or path == '':
            return shop_directory()
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


# ── Shop landing chat (creator-specific KB) ──────────────────────────────────
_SHOP_CHAT_CACHE: dict = {}


def _tok(s: str) -> list:
    s = (s or '').lower()
    return [t for t in re.split(r'[^a-z0-9]+', s) if len(t) > 1]


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


def _build_shop_chat_kb(creator_id: str) -> list:
    """Build a creator-scoped product KB from published collages + posts."""
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    try:
        # Aggregate click_log by ASIN for this creator from both landing-page
        # slugs and post slugs to support ranking by real engagement.
        clicks_by_asin = {}
        click_rows = conn.execute(
            """
            SELECT cl.asin, COUNT(*) AS c
            FROM click_log cl
            JOIN collages c ON c.slug = cl.slug
            WHERE COALESCE(c.creator_id, 'everydaywithsteph') = ?
            GROUP BY cl.asin
            """,
            (creator_id,),
        ).fetchall()
        for r in click_rows:
            asin = (r['asin'] or '').strip()
            if asin:
                clicks_by_asin[asin] = clicks_by_asin.get(asin, 0) + int(r['c'] or 0)

        post_click_rows = conn.execute(
            """
            SELECT cl.asin, COUNT(*) AS c
            FROM click_log cl
            JOIN posts p ON p.slug = cl.slug
            WHERE COALESCE(p.creator_id, 'everydaywithsteph') = ?
            GROUP BY cl.asin
            """,
            (creator_id,),
        ).fetchall()
        for r in post_click_rows:
            asin = (r['asin'] or '').strip()
            if asin:
                clicks_by_asin[asin] = clicks_by_asin.get(asin, 0) + int(r['c'] or 0)

        kb_by_asin = {}

        collage_rows = conn.execute(
            """
            SELECT slug, products_json
            FROM collages
            WHERE COALESCE(creator_id, 'everydaywithsteph') = ?
              AND COALESCE(status, 'published') = 'published'
            ORDER BY created_at DESC
            """,
            (creator_id,),
        ).fetchall()
        for row in collage_rows:
            slug = (row['slug'] or '').strip()
            try:
                products = json.loads(row['products_json'] or '[]')
            except (json.JSONDecodeError, TypeError):
                products = []
            for p in products:
                asin = (p.get('asin') or '').strip()
                if not asin:
                    continue
                item = kb_by_asin.setdefault(asin, {
                    'asin': asin,
                    'name': '',
                    'brand': '',
                    'price': '',
                    'image': '',
                    'link': '',
                    'sources': set(),
                    'collage_slugs': set(),
                    'clicks': 0,
                })
                item['name'] = item['name'] or (p.get('product_name') or p.get('name') or '')
                item['brand'] = item['brand'] or (p.get('company_name') or p.get('brand') or '')
                item['price'] = item['price'] or _format_display_price(p.get('price') or '')
                item['image'] = item['image'] or (p.get('image_encoded_string') or '')
                item['link'] = item['link'] or (p.get('attribution_link') or '')
                item['sources'].add('collage')
                if slug:
                    item['collage_slugs'].add(slug)
                item['clicks'] = clicks_by_asin.get(asin, 0)

        post_rows = conn.execute(
            """
            SELECT asin, product_name, product_brand, product_price, product_image, smart_link, collection_slug
            FROM posts
            WHERE COALESCE(creator_id, 'everydaywithsteph') = ?
              AND status != 'archived'
            ORDER BY created_at DESC
            LIMIT 2000
            """,
            (creator_id,),
        ).fetchall()
        for r in post_rows:
            asin = (r['asin'] or '').strip()
            if not asin:
                continue
            item = kb_by_asin.setdefault(asin, {
                'asin': asin,
                'name': '',
                'brand': '',
                'price': '',
                'image': '',
                'link': '',
                'sources': set(),
                'collage_slugs': set(),
                'clicks': 0,
            })
            # Prefer post metadata if the current value is missing
            item['name'] = item['name'] or (r['product_name'] or '')
            item['brand'] = item['brand'] or (r['product_brand'] or '')
            item['price'] = item['price'] or _format_display_price(r['product_price'] or '')
            item['image'] = item['image'] or (r['product_image'] or '')
            item['link'] = item['link'] or (r['smart_link'] or '')
            item['sources'].add('post')
            if r['collection_slug']:
                item['collage_slugs'].add(r['collection_slug'])
            item['clicks'] = clicks_by_asin.get(asin, 0)

        out = []
        for item in kb_by_asin.values():
            sources = sorted(list(item.pop('sources')))
            collage_slugs = sorted(list(item.pop('collage_slugs')))
            item['sources'] = sources
            item['collage_slugs'] = collage_slugs
            out.append(item)
        return out
    finally:
        conn.close()


def _get_shop_chat_kb(creator_id: str) -> list:
    key = (creator_id or 'everydaywithsteph').strip() or 'everydaywithsteph'
    now = time.time()
    cached = _SHOP_CHAT_CACHE.get(key)
    if cached and now < cached['expires']:
        return cached['items']
    items = _build_shop_chat_kb(key)
    _SHOP_CHAT_CACHE[key] = {
        'items': items,
        'expires': now + 1800,  # 30 minutes
    }
    return items


def _rank_shop_kb(query: str, items: list, current_slug: str = '') -> list:
    """Relevancy first, then click count, then current-page product bias."""
    q_tokens = set(_tok(query))
    current_slug = (current_slug or '').strip().lower()

    def _score(it: dict) -> tuple:
        text = ' '.join([
            it.get('name', ''),
            it.get('brand', ''),
            it.get('price', ''),
            ' '.join(it.get('sources', [])),
        ]).lower()
        # Relevancy score
        overlap = sum(1 for t in q_tokens if t in text)
        title_phrase = 1 if query.lower().strip() and query.lower().strip() in (it.get('name', '').lower()) else 0
        relevancy = overlap + (2 * title_phrase)

        # Tie-breakers
        clicks = int(it.get('clicks') or 0)
        on_current_page = 1 if current_slug and current_slug in set(it.get('collage_slugs', [])) else 0
        return (relevancy, clicks, on_current_page)

    ranked = sorted(items, key=_score, reverse=True)
    return ranked


@app.route('/api/shop/chat', methods=['POST'])
def shop_chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    creator_id = (data.get('creator_id') or 'everydaywithsteph').strip() or 'everydaywithsteph'
    slug = (data.get('slug') or '').strip().lower()
    if not user_message:
        return jsonify({'error': 'message is required'}), 400

    try:
        kb = _get_shop_chat_kb(creator_id)
        if not kb:
            return jsonify({
                'reply': "I don’t have product data loaded yet. Please check back in a bit 💕",
                'products': [],
            })

        ranked = _rank_shop_kb(user_message, kb, current_slug=slug)
        # Keep prompt compact: send top 20 candidates
        candidates = ranked[:20]
        lines = []
        for i, p in enumerate(candidates):
            lines.append(
                f"[{i}] ASIN:{p.get('asin','')} | {p.get('name','')[:100]} | "
                f"Brand:{p.get('brand','')} | Price:{p.get('price','')} | "
                f"Clicks:{p.get('clicks',0)} | Sources:{','.join(p.get('sources', []))}"
            )
        catalog = '\n'.join(lines)

        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=280,
            system=(
                "You are a shopping assistant for a creator storefront. "
                "Recommend only from the provided candidate products. "
                "Prioritize query relevancy. If multiple relevant options exist, "
                "prefer higher-click products. Keep tone concise, friendly, and helpful. "
                "Return exactly two lines:\n"
                "REPLY: <short shopper-facing text>\n"
                "PRODUCTS: <comma-separated candidate indexes (max 3)>"
            ),
            messages=[{
                'role': 'user',
                'content': (
                    f"User query: {user_message}\n"
                    f"Current landing slug: {slug or '(none)'}\n"
                    f"Candidates:\n{catalog}"
                )
            }],
        )
        raw = (msg.content[0].text or '').strip()
        text_reply = "Here are a few picks you might love."
        picked = []
        for ln in raw.splitlines():
            if ln.strip().upper().startswith('REPLY:'):
                text_reply = ln.split(':', 1)[1].strip() or text_reply
            if ln.strip().upper().startswith('PRODUCTS:'):
                idx_part = ln.split(':', 1)[1].strip()
                for bit in idx_part.split(','):
                    bit = bit.strip()
                    if bit.isdigit():
                        idx = int(bit)
                        if 0 <= idx < len(candidates):
                            picked.append(candidates[idx])
        if not picked:
            picked = candidates[:3]

        out_products = []
        creator = db_schema.get_creator(creator_id)
        tag = creator.get('amazon_tag') or 'mommymedeals-20'
        for p in picked[:3]:
            asin = (p.get('asin') or '').strip()
            link = (p.get('link') or '').strip()
            if not link and asin:
                try:
                    smart = _make_smart_link(
                        asin=asin,
                        network='amazon',
                        utm_source='shop-chat',
                        utm_medium='chat',
                        utm_campaign=slug or 'shop',
                        utm_content='shop-chat-reco',
                        utm_term=asin.lower(),
                    )
                    link = (smart or {}).get('genius_url') or (smart or {}).get('affiliate_url') or ''
                except Exception:
                    link = ''
            if not link and asin:
                link = f"https://www.amazon.com/dp/{asin}?tag={tag}"

            out_products.append({
                'asin': asin,
                'name': p.get('name') or f'Amazon Product {asin}',
                'price': p.get('price') or '',
                'image': p.get('image') or '',
                'retailer': 'Amazon',
                'link': link,
            })

        return jsonify({'reply': text_reply, 'products': out_products})
    except Exception as e:
        logging.error(f"[SHOP_CHAT] failed: {e}")
        return jsonify({
            'reply': "I hit a snag, but here are top picks from this creator.",
            'products': [],
            'error': str(e),
        }), 500

@app.route('/')
def index():
    return render_template('dashboard.html')

# ARCHIVED — see /archive/routes/

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/dashboard/upload_csv', methods=['POST'])
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

@app.route('/archer/products')
def archer_products():
    return render_template('archer_products.html')

# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/


# ARCHIVED — see /archive/routes/

@app.route('/archer/search')
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

@app.route('/archer/generate_link', methods=['POST'])
def archer_generate_link():
    """Generate a live Archer attribution link for a given ASIN."""
    from product_api import ArcherAPI
    data = request.get_json() or {}
    asin = data.get('asin', '').strip()
    label = data.get('label', asin)
    if not asin:
        return jsonify({'error': 'asin is required'}), 400
    a = ArcherAPI()
    result = a.generate_link(asin, label=label)
    if not result:
        return jsonify({'error': 'Link generation failed'}), 500
    return jsonify(result)

@app.route('/archer/collage')
def archer_collage():
    return render_template('archer_collage.html')

@app.route('/archer/product/<asin>')
def archer_get_product(asin):
    from product_api import ArcherAPI
    from utils.asin import extract_asin
    resolved = extract_asin(asin)
    if resolved:
        asin = resolved
    a = ArcherAPI()
    product = a.get_by_asins([asin])

    # If found in cache but no image, force a live lookup to backfill
    if product and not product[0].get('image_encoded_string'):
        product = []

    if not product:
        try:
            data = a.get_product(asin)
            if data:
                img = data.get("image_encoded_string", "")
                if img:
                    conn = a._db_connect()
                    conn.execute("UPDATE products SET image_encoded_string=? WHERE asin=?", (img, asin))
                    conn.commit()
                    conn.close()
                p = {
                    "asin": data.get("ASIN") or asin,
                    "product_name": data.get("product_name"),
                    "company_name": data.get("company_name"),
                    "price": data.get("price"),
                    "commission_payout": data.get("commission_payout_aff"),
                    "image_encoded_string": img,
                    "product_category": data.get("product_category"),
                }
                if data.get("live_price") is not None:
                    p["live_price"] = data["live_price"]
                return jsonify({"product": p})
        except Exception as e:
            logging.error(f"[ARCHER] Product lookup failed for {asin}: {e}")
        return jsonify({"error": "Product not found"}), 404
    p = product[0]
    if not p.get("price"):
        try:
            from utils.crawlbase import get_live_price
            live = get_live_price(asin)
            if live is not None:
                p["live_price"] = live
        except Exception:
            pass
    return jsonify({"product": p})

@app.route('/archer/generate_caption', methods=['POST'])
def archer_generate_caption():
    data = request.get_json() or {}
    products_str = data.get('products', '')
    product_list = data.get('product_list', [])   # [{asin, product_name, brand, ...}, ...]
    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=200,
            system=STEPH_CAPTION_PROMPT,
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


@app.route('/archer/generate_organic_posts', methods=['POST'])
def archer_generate_organic_posts():
    """Generate 20 organic FB Group post variations for Steph.

    Patches applied:
      - Pre-generates ONE URLGenius smart link per unique ASIN (cuts 20 API calls → 1-3).
      - Asks Claude for product_index per post; routes use it to pick the matching link.
      - Falls back to round-robin (i % len) if Claude omits product_index.
      - Regex JSON extraction fallback for stray text around the JSON block.
      - Surfaces the real link_result['label'] as urlgenius_tag (not a fake string).
    """
    from datetime import datetime as _dt
    data = request.get_json() or {}
    product_list = data.get('product_list', [])  # [{asin, product_name, brand, price, commission}, ...]
    if not product_list:
        return jsonify({'error': 'product_list is required'}), 400

    mmdd = _dt.now().strftime('%m%d')
    n_products = len(product_list)
    pl = '\n'.join([
        f"[{idx}] {p.get('product_name','')[:60]} by {p.get('brand','')} · "
        f"{p.get('price','')} · {p.get('commission','')} commission · ASIN: {p.get('asin','')}"
        for idx, p in enumerate(product_list)
    ])

    # ── Pre-generate one URLGenius smart link per unique ASIN (rate-limit safe) ──
    link_cache: dict = {}
    seen_asins = []
    for p in product_list:
        asin = (p.get('asin') or '').strip()
        if not asin or asin in link_cache:
            continue
        seen_asins.append(asin)
        brand_raw = (p.get('brand') or 'brand').lower()
        brand_short = re.sub(r'[^a-z0-9]', '', brand_raw.split()[0] if brand_raw.split() else 'brand')[:10]
        name_words = re.sub(r'[^a-z0-9 ]', '', (p.get('product_name') or 'product').lower()).split()
        product_short = '-'.join(name_words[:2])[:15] or 'product'
        campaign = f"{brand_short}-{product_short}-{mmdd}"
        try:
            link_cache[asin] = _make_smart_link(
                asin=asin, network='amazon',
                utm_source='fb-group', utm_medium='organic',
                utm_campaign=campaign,
            )
        except Exception as e:
            logging.warning(f'[ORGANIC] Smart link failed for {asin}: {e}')
            link_cache[asin] = {
                'genius_url': f'https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}',
                'affiliate_url': f'https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}',
                'label': f'fb-group_organic_{campaign}_{mmdd}',
                'urlgenius': False,
            }

    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=6000,
            system=STEPH_ORGANIC_POSTS_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"Products (use product_index 0..{n_products - 1} to reference each):\n"
                    f"{pl}\n\nDate code: {mmdd}\n\n"
                    f"Generate 20 organic Facebook Group post variations. "
                    f"Cycle through every product so all {n_products} appear roughly evenly."
                )
            }]
        )
        raw = message.content[0].text.strip().replace('```json', '').replace('```', '').strip()

        # Resilient JSON parse — fall back to regex extraction of the JSON object
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                raise
            parsed = json.loads(m.group(0))
        posts_raw = parsed.get('posts', [])

        posts = []
        for i, post in enumerate(posts_raw):
            # Map Claude's product_index → product → cached smart link
            try:
                pidx = int(post.get('product_index', i % n_products))
            except (TypeError, ValueError):
                pidx = i % n_products
            if pidx < 0 or pidx >= n_products:
                pidx = i % n_products
            product = product_list[pidx]
            asin = (product.get('asin') or '').strip()
            link_result = link_cache.get(asin, {
                'genius_url': f'https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}' if asin else '',
                'affiliate_url': f'https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}' if asin else '',
                'label': '',
                'urlgenius': False,
            })

            posts.append({
                'angle': post.get('angle', ''),
                'copy': post.get('copy', ''),
                'image_note': post.get('image_note', ''),
                'product_index': pidx,
                'product_name': product.get('product_name') or product.get('name') or '',
                'brand': product.get('brand') or '',
                'asin': asin,
                'affiliate_url': link_result.get('affiliate_url', ''),
                'genius_url': link_result.get('genius_url', ''),
                'urlgenius_tag': link_result.get('label', ''),
                'urlgenius_active': link_result.get('urlgenius', False),
            })

        return jsonify({'posts': posts, 'product_count': n_products, 'unique_asins': len(seen_asins)})
    except Exception as e:
        logging.error(f'[ORGANIC] Post generation failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/archer/generate_posts', methods=['POST'])
def archer_generate_posts():
    """Content builder v2 — returns Claude-generated copy WITHOUT pre-built links.
    Frontend creates smart links on-demand via /urlgenius/smart_link after reviewing UTM tags.

    Mode B: 1 post per product, different angle per product.
    Mode C: 1 collection post + product taglines.
    """
    data = request.get_json() or {}
    mode = data.get('mode', 'b')
    product_list = data.get('product_list', [])
    if not product_list:
        return jsonify({'error': 'product_list is required'}), 400

    n = len(product_list)
    pl = '\n'.join([
        f"[{i}] ASIN:{p.get('asin','')} | {(p.get('product_name') or p.get('name',''))[:50]}"
        f" by {p.get('brand','')} · {p.get('price','')} · {p.get('commission','')} commission"
        for i, p in enumerate(product_list)
    ])

    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

        if mode == 'b':
            system = (
                "You generate organic Facebook Group posts for Steph (@EverydaywithSteph / Mommy & Me Collective). "
                "Voice: warm, mom-to-mom, texting best friend about a deal. 1-2 emojis max. "
                "Direct. Mentions price or benefit. 2-5 sentences. Never sounds like an ad.\n\n"
                "Return ONLY valid JSON:\n"
                '{"posts":[{"asin":"string","angle":"lowercase-hyphenated-slug max 20 chars",'
                '"copy":"full post text","image_note":"brief ideal image description"}]}\n\n'
                "Generate exactly N posts — one per product. Each must use a DIFFERENT angle from: "
                "deal-price, mom-rec, social-proof, seasonal, gift-idea, problem-solve, scarcity, "
                "discovery, value-frame, bundle-pair, before-after, community-reaction, "
                "everyday-essential, back-to-camp, educational."
            ).replace('N', str(n))
            user_msg = f"Generate exactly {n} post{'s' if n > 1 else ''}, one per product.\nProducts:\n{pl}"
            msg = client.messages.create(
                model='claude-sonnet-4-6', max_tokens=4000, system=system,
                messages=[{'role': 'user', 'content': user_msg}]
            )
            raw = msg.content[0].text.strip().replace('```json', '').replace('```', '').strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if not m:
                    raise
                parsed = json.loads(m.group(0))

            # Persist generated posts to the queue (Branch 2B). Each post becomes
            # a draft row joinable to clicks/earnings via posts.slug ↔ click_log.slug.
            import posts as _posts
            creator_id = (data.get('creator_id') or 'everydaywithsteph').strip()
            collection_slug = (data.get('collection_slug') or '').strip().lower() or None
            persisted = []
            asin_to_product = {(p.get('asin') or '').strip(): p for p in product_list}
            for raw_post in parsed.get('posts', []):
                asin = (raw_post.get('asin') or '').strip()
                product = asin_to_product.get(asin, {})
                try:
                    saved = _posts.create_post(
                        creator_id=creator_id,
                        asin=asin,
                        angle=raw_post.get('angle', ''),
                        copy=raw_post.get('copy', ''),
                        image_note=raw_post.get('image_note', ''),
                        collection_slug=collection_slug,
                        status='draft',
                        product_name=product.get('product_name') or product.get('name') or '',
                        product_brand=product.get('brand') or product.get('company_name') or '',
                        product_price=product.get('price') or '',
                        product_image=product.get('image_encoded_string') or '',
                    )
                    persisted.append(saved)
                except Exception as _e:
                    logging.warning(f"[GENERATE_POSTS] persist failed for {asin}: {_e}")
                    persisted.append({
                        'angle': raw_post.get('angle', ''),
                        'copy': raw_post.get('copy', ''),
                        'image_note': raw_post.get('image_note', ''),
                        'asin': asin,
                    })
            return jsonify({
                'mode': 'b',
                'posts': parsed.get('posts', []),
                'persisted_posts': persisted,
                'persisted_count': len([p for p in persisted if p.get('id')]),
            })

        elif mode == 'c':
            collection_name = data.get('collection_name', 'Collection')
            collection_slug = data.get('collection_slug', 'collection')
            # Public landing-page URL on the shop subdomain (configured via SHOP_SUBDOMAIN env)
            url = f"https://{SHOP_SUBDOMAIN}/{collection_slug}"
            system = (
                f"You build themed shoppable collection posts for Steph (@EverydaywithSteph). "
                "Voice: warm, enthusiastic, mom-to-mom. 2-4 sentences. Naturally mentions the URL.\n\n"
                "Return ONLY valid JSON:\n"
                '{"angle":"lowercase-hyphenated-slug max 20 chars","copy":"collection Facebook Group post",'
                '"image_note":"ideal collage image description",'
                '"product_taglines":[{"asin":"string","tagline":"one sentence why this product belongs"}]}\n\n'
                "angle must be lowercase-hyphenated (used in utm_content as organic_ANGLE_collection)."
            )
            user_msg = f'Theme: "{collection_name}"\nURL: {url}\nProducts:\n{pl}\n\nNaturally mention {url} in the post.'
            msg = client.messages.create(
                model='claude-sonnet-4-6', max_tokens=2000, system=system,
                messages=[{'role': 'user', 'content': user_msg}]
            )
            raw = msg.content[0].text.strip().replace('```json', '').replace('```', '').strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if not m:
                    raise
                parsed = json.loads(m.group(0))
            return jsonify({'mode': 'c', 'collection': parsed, 'url': url})

        else:
            return jsonify({'error': f'Unknown mode: {mode}'}), 400

    except Exception as e:
        logging.error(f'[GENERATE_POSTS] mode={mode} failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/archer/generate_campaign_package', methods=['POST'])
def archer_generate_campaign_package():
    """Generate a 5-layer Meta ad campaign package + paste-ready Ryze MCP prompt.

    Same patches as /archer/generate_organic_posts:
      - One URLGenius smart link per unique ASIN, reused across all variants.
      - Resilient JSON parse with regex fallback.
      - Real link labels surfaced (not fabricated strings).
    """
    from datetime import datetime as _dt
    from urllib.parse import urlencode
    data = request.get_json() or {}
    product_list = data.get('product_list', [])
    layers = data.get('layers', [1, 2, 3, 4, 5])
    slug = (data.get('slug') or '').strip()
    # Collection-as-CTA: when caller provides a collection_slug, all 5 layers route
    # to the published landing page at https://<SHOP_SUBDOMAIN>/<collection_slug>
    # with utm_content suffix '_collection' instead of per-product Amazon links.
    collection_slug = (data.get('collection_slug') or '').strip().lower()

    if not product_list:
        return jsonify({'error': 'product_list is required'}), 400

    mmdd = _dt.now().strftime('%m%d')
    pl = '\n'.join([
        f"[{idx}] {p.get('product_name','')[:60]} by {p.get('brand','')} · "
        f"{p.get('price','')} · {p.get('commission','')} commission · ASIN: {p.get('asin','')}"
        for idx, p in enumerate(product_list)
    ])

    asin = (product_list[0].get('asin') or '').strip()
    if not asin:
        return jsonify({'error': 'product_list[0] must contain a valid ASIN'}), 400
    brand_raw = (product_list[0].get('brand') or 'brand').lower()
    brand_short = re.sub(r'[^a-z0-9]', '', brand_raw.split()[0] if brand_raw.split() else 'brand')[:10]

    # ── Pre-generate one Archer/URLGenius link per (ASIN, layer) — bounded calls ──
    # Variants within a layer share the same link (different utm_term per variant
    # would multiply API calls 3×; layer-level granularity is enough for reporting).
    link_cache: dict = {}

    # Build collection context line if we're routing to a landing page
    collection_context = ''
    if collection_slug:
        collection_context = (
            f"\nCTA destination: a curated collection landing page bundling all "
            f"{len(product_list)} products at https://{SHOP_SUBDOMAIN}/{collection_slug}. "
            f"Headlines and copy should reference the curated bundle (multiple picks, "
            f"not a single product) — angles like 'shop my picks', 'whole collection', "
            f"'mom-curated bundle' work well.\n"
        )

    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=8000,
            system=STEPH_CAMPAIGN_PACKAGE_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"Products:\n{pl}\n{collection_context}\nLayers to include: {layers}\n\n"
                    f"Generate the 5-layer campaign package."
                )
            }]
        )
        raw = message.content[0].text.strip().replace('```json', '').replace('```', '').strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                raise
            parsed = json.loads(m.group(0))
        layers_data = parsed.get('layers', [])

        # Normalize layer_num: stable-sort by claimed layer_num then re-assign 1..N
        # so we always return a clean L1..L5 sequence even if Claude duplicates,
        # skips, or re-orders them.
        try:
            layers_data = sorted(layers_data, key=lambda l: int(l.get('layer_num', 99) or 99))
        except (TypeError, ValueError):
            pass
        for idx, layer in enumerate(layers_data, start=1):
            layer['layer_num'] = idx

        for layer in layers_data:
            layer_num = layer.get('layer_num', 0)

            if collection_slug:
                # ── Collection CTA path: build a direct UTM URL pointing at the
                # public landing page. One link per layer, shared across variants.
                campaign = f"{slug or brand_short}-collection-l{layer_num}-{mmdd}"
                cache_key = (collection_slug, layer_num)
                if cache_key not in link_cache:
                    qs = urlencode({
                        'utm_source':   'fb-ad',
                        'utm_medium':   'paid_social',
                        'utm_campaign': campaign,
                        'utm_content':  f'l{layer_num}_collection',
                        'utm_term':     f'l{layer_num}',
                    })
                    link_cache[cache_key] = {
                        'genius_url':    f'https://{SHOP_SUBDOMAIN}/{collection_slug}?{qs}',
                        'affiliate_url': '',
                        'label':         f'collection-{collection_slug}-l{layer_num}',
                        'urlgenius':     False,
                    }
                link_result = link_cache[cache_key]
            else:
                # ── Single-product CTA path (original behavior): URLGenius wrap → Amazon
                cache_key = (asin, layer_num)
                if asin and cache_key not in link_cache:
                    campaign = f"{slug or brand_short}-{asin.lower()[:6]}-l{layer_num}-{mmdd}"
                    try:
                        link_cache[cache_key] = _make_smart_link(
                            asin=asin, network='archer',
                            utm_source='fb-ad', utm_medium='dark',
                            utm_campaign=campaign,
                            utm_term=f'l{layer_num}',
                        )
                    except Exception as e:
                        logging.warning(f'[CAMPAIGN] Smart link failed for {asin} L{layer_num}: {e}')
                        link_cache[cache_key] = {
                            'genius_url': f'https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}',
                            'affiliate_url': '',
                            'label': '',
                            'urlgenius': False,
                        }
                link_result = link_cache.get(cache_key, {
                    'genius_url': '', 'affiliate_url': '', 'label': '', 'urlgenius': False,
                })

            for i, variant in enumerate(layer.get('variants', [])):
                var_letter = ['a', 'b', 'c'][i] if i < 3 else str(i)
                variant['attribution_url'] = link_result.get('genius_url', '')
                variant['archer_url'] = link_result.get('affiliate_url', '')
                variant['urlgenius_tag'] = link_result.get('label', '')
                variant['var_letter'] = var_letter
                variant['cta_type'] = 'collection' if collection_slug else 'product'

        # Build paste-ready Ryze MCP prompt
        layer_lines = []
        for l in layers_data:
            var_block = '\n'.join([
                f"    Variant {v.get('label','')}: Headline: {v.get('headline','')} | "
                f"Body: {v.get('primary_text','')} | CTA: {v.get('cta','Shop Now')} | "
                f"URL: {v.get('attribution_url','')}"
                for v in l.get('variants', [])
            ])
            layer_lines.append(
                f"  Layer {l.get('layer_num','')}: {l.get('name','')}\n"
                f"  Budget: {l.get('daily_budget_range','')}/day | "
                f"Advantage+: {'ON' if l.get('advantage_plus') else 'OFF'}\n"
                f"  Audience: {l.get('audience','')}\n{var_block}\n"
                f"  Creative: {l.get('creative_direction','')}"
            )

        ryze_prompt = (
            f"Using the Ryze MCP connected to Steph's Meta account (act_573934886369270), "
            f"create the following ad campaigns for ASIN {asin} "
            f"({product_list[0].get('brand','')} — {product_list[0].get('product_name','')}).\n\n"
            f"For each campaign: OUTCOME_TRAFFIC objective, CBO at campaign level, "
            f"Advantage+ as specified, mobile-first placements.\n\n"
            f"CAMPAIGNS TO CREATE:\n\n" + '\n\n'.join(layer_lines) +
            f"\n\nCreate all campaigns in PAUSED status for review before activating. "
            f"Confirm each campaign ID after creation."
        )

        # Auto-tag the collection as 'paid' since it was just used as an ad CTA
        if collection_slug:
            try:
                db_schema.add_campaign_type_to_collage(collection_slug, 'paid')
            except Exception as _e:
                logging.warning(f"[CAMPAIGN] tag paid failed for {collection_slug}: {_e}")

        return jsonify({
            'layers': layers_data,
            'ryze_prompt': ryze_prompt,
            'asin': asin,
            'product': product_list[0] if product_list else {},
            'unique_links': len(link_cache),
            'cta_type': 'collection' if collection_slug else 'product',
            'collection_slug': collection_slug or None,
            'collection_url': f'https://{SHOP_SUBDOMAIN}/{collection_slug}' if collection_slug else None,
        })
    except Exception as e:
        logging.error(f'[CAMPAIGN] Package generation failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/archer/collage/save', methods=['POST'])
def archer_save_collage():
    """Save (or update) a collage.

    Branch 2B behavior change:
      status='draft'     → SKIP Archer attribution-link generation (saves API
                            quota during preview iteration)
      status='published' → Generate Archer links for any product missing one
                            (existing default behavior)
    Drafts are 404 publicly and viewable only via /shop/<slug>?preview=1.
    """
    from product_api import ArcherAPI
    data = request.get_json() or {}
    slug = data.get('slug', '').strip().lower().replace(' ', '-')
    if not slug or not data.get('products'):
        return jsonify({'error': 'slug and products required'}), 400

    creator_id = (data.get('creator_id') or 'everydaywithsteph').strip()
    status = (data.get('status') or 'published').strip()

    a = ArcherAPI()
    products = data.get('products', [])
    if status == 'published':
        for p in products:
            asin = p.get('asin', '')
            if asin and not p.get('attribution_link'):
                link = a.generate_link(asin, label=f"{slug}-{asin.lower()}")
                if link:
                    p['attribution_link'] = link.get('attribution_link') or link.get('url') or ''

    # Preserve campaign_types if the collage already exists (e.g. previously
    # tagged 'paid' from Ad Builder use; we don't want to clobber that on re-save).
    conn = a._db_connect()
    existing = conn.execute(
        "SELECT campaign_types FROM collages WHERE slug = ?", (slug,)
    ).fetchone()
    try:
        prior_types = json.loads(existing[0]) if existing and existing[0] else []
        if not isinstance(prior_types, list):
            prior_types = []
    except (json.JSONDecodeError, TypeError):
        prior_types = []
    # Mode C save always implies organic usage
    merged_types = list({*prior_types, 'organic'})

    conn.execute("""
        INSERT OR REPLACE INTO collages
        (slug, products_json, layout, theme, caption, direct_to_amazon,
         creator_id, status, campaign_types, hero_title, hero_subtitle, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        slug,
        json.dumps(products),
        data.get('layout', 'layout-2'),
        data.get('theme', 'coral'),
        data.get('caption', ''),
        1 if data.get('direct_to_amazon') else 0,
        creator_id,
        status,
        json.dumps(merged_types),
        data.get('hero_title', ''),
        data.get('hero_subtitle', ''),
    ))
    conn.commit()
    conn.close()

    is_draft = status != 'published'
    return jsonify({
        'url': f'/shop/{slug}' + ('?preview=1' if is_draft else ''),
        'public_url': (
            f'https://{SHOP_SUBDOMAIN}/{slug}'
            if not is_draft
            else f'/shop/{slug}?preview=1'
        ),
        'slug': slug,
        'creator_id': creator_id,
        'status': status,
        'is_draft': is_draft,
        'campaign_types': merged_types,
    })


@app.route('/archer/collage/publish', methods=['POST'])
def archer_collage_publish():
    """Promote a draft collage to published. Generates Archer attribution links
    for products that don't have them yet, then flips status to 'published'.

    Body: { "slug": "..." }
    """
    from product_api import ArcherAPI
    data = request.get_json() or {}
    slug = (data.get('slug') or '').strip().lower()
    if not slug:
        return jsonify({'error': 'slug is required'}), 400

    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM collages WHERE slug = ?", (slug,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'collection not found'}), 404

    try:
        products = json.loads(row['products_json'] or '[]')
    except (json.JSONDecodeError, TypeError):
        products = []

    # Backfill Archer attribution links on products that lack them
    for p in products:
        asin = p.get('asin', '')
        if asin and not p.get('attribution_link'):
            try:
                link = a.generate_link(asin, label=f"{slug}-{asin.lower()}")
                if link:
                    p['attribution_link'] = (
                        link.get('attribution_link') or link.get('url') or ''
                    )
            except Exception as _e:
                logging.warning(f"[PUBLISH] Archer link gen failed for {asin}: {_e}")

    conn.execute(
        "UPDATE collages SET products_json = ?, status = 'published' WHERE slug = ?",
        (json.dumps(products), slug),
    )
    conn.commit()
    conn.close()
    return jsonify({
        'slug': slug,
        'status': 'published',
        'public_url': f'https://{SHOP_SUBDOMAIN}/{slug}',
    })

@app.route('/archer/collage/<slug>', methods=['GET'])
def archer_collage_get(slug):
    """Return one collection's full record (used by Ad Builder auto-load
    when ?collection=<slug> deep-link is hit, and by the Mode C edit flow)."""
    from product_api import ArcherAPI
    conn = ArcherAPI()._db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM collages WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    out = dict(row)
    try:
        out['products'] = json.loads(out.pop('products_json') or '[]')
    except (json.JSONDecodeError, TypeError):
        out['products'] = []
    try:
        out['campaign_types'] = (
            json.loads(out['campaign_types']) if out.get('campaign_types') else []
        )
    except (json.JSONDecodeError, TypeError):
        out['campaign_types'] = []
    return jsonify({'collage': out})


@app.route('/archer/collages')
def archer_list_collages():
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT slug, theme, layout, created_at, click_count, products_json, "
        "creator_id, status, campaign_types "
        "FROM collages "
        "WHERE COALESCE(status,'published') != 'archived' "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    collages = []
    for r in rows:
        products = json.loads(r['products_json'] or '[]')
        try:
            ctypes = json.loads(r['campaign_types']) if r['campaign_types'] else ['organic']
            if not isinstance(ctypes, list):
                ctypes = ['organic']
        except (json.JSONDecodeError, TypeError):
            ctypes = ['organic']
        collages.append({
            'slug':           r['slug'],
            'theme':          r['theme'],
            'layout':         r['layout'],
            'created_at':     r['created_at'][:10] if r['created_at'] else '',
            'click_count':    r['click_count'] or 0,
            'product_count':  len(products),
            'creator_id':     r['creator_id'] or 'everydaywithsteph',
            'status':         r['status'] or 'published',
            'campaign_types': ctypes,
        })
    return jsonify({'collages': collages})

@app.route('/shop/<slug>')
def shop_landing(slug):
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM collages WHERE slug=?", (slug,)).fetchone()
    conn.close()
    if not row:
        return "Page not found", 404
    collage = dict(row)
    # Drafts only viewable via ?preview=1
    if (collage.get('status') or 'published') != 'published' and request.args.get('preview') != '1':
        return "Page not found", 404

    products = json.loads(collage.get('products_json') or '[]')
    for p in products:
        p['price_display'] = _format_display_price(p.get('price') or '')
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
    is_preview = request.args.get('preview') == '1'

    return render_template('shop_landing.html',
        collage=collage,
        products=products,
        themes=THEMES,
        pixel_id=pixel_id,
        creator=creator,
        seo={
            'title':         page_title,
            'description':   page_description,
            'og_image':      og_image,
            'canonical_url': canonical_url,
        },
        is_preview=is_preview,
    )

@app.route('/shop/')
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
        creator = creators_by_id.get(r['creator_id'] or 'everydaywithsteph', {})
        items.append({
            'slug':          r['slug'],
            'title':         r['hero_title'] or (r['slug'] or '').replace('-', ' ').title(),
            'subtitle':      r['hero_subtitle'] or (r['caption'] or '')[:160],
            'theme':         r['theme'],
            'cover_image':   cover_img,
            'product_count': len(products),
            'click_count':   r['click_count'] or 0,
            'created_at':    (r['created_at'] or '')[:10],
            'creator_id':    r['creator_id'] or 'everydaywithsteph',
            'creator_handle': creator.get('handle') or '@creator',
            'creator_name':   creator.get('display_name') or 'Creator',
        })

    return render_template(
        'shop_directory.html',
        items=items,
        themes=THEMES,
        canonical_url=f'https://{SHOP_SUBDOMAIN}/',
        shop_subdomain=SHOP_SUBDOMAIN,
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
        SELECT id, slug, asin, angle, copy, collection_slug, status, smart_link,
               product_name, product_brand, product_price, product_image,
               creator_id, created_at, posted_at
        FROM posts
        WHERE status != 'archived'
        ORDER BY COALESCE(posted_at, created_at) DESC
        LIMIT 400
        """
    ).fetchall()
    conn.close()

    creators_by_id = {c['id']: c for c in db_schema.list_creators()}
    items = []
    for r in rows:
        creator = creators_by_id.get(r['creator_id'] or 'everydaywithsteph', {})
        copy = (r['copy'] or '').strip()
        items.append({
            'id': r['id'],
            'slug': r['slug'] or '',
            'asin': r['asin'] or '',
            'angle': r['angle'] or '',
            'copy': copy,
            'copy_excerpt': (copy[:180] + '…') if len(copy) > 180 else copy,
            'collection_slug': r['collection_slug'] or '',
            'status': r['status'] or 'draft',
            'smart_link': r['smart_link'] or '',
            'product_name': r['product_name'] or (r['asin'] or 'Product'),
            'product_brand': r['product_brand'] or '',
            'product_price': _format_display_price(r['product_price'] or ''),
            'product_image': r['product_image'] or '',
            'creator_id': r['creator_id'] or 'everydaywithsteph',
            'creator_handle': creator.get('handle') or '@creator',
            'created_at': (r['created_at'] or '')[:10],
            'posted_at': (r['posted_at'] or '')[:10] if r['posted_at'] else '',
            'shop_url': f"https://{SHOP_SUBDOMAIN}/{r['collection_slug']}" if r['collection_slug'] else '',
        })

    return render_template(
        'shop_posts.html',
        items=items,
        canonical_url=f'https://{SHOP_SUBDOMAIN}/posts',
        shop_subdomain=SHOP_SUBDOMAIN,
    )


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
    for slug, updated in rows:
        lastmod = (updated or '')[:10]
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
def archer_post_update(post_id):
    """Update a single post's editable fields (copy, angle, status, UTMs, smart_link)."""
    import posts as _posts
    body = request.get_json() or {}
    saved = _posts.update_post(post_id, body)
    if not saved:
        return jsonify({'error': 'post not found'}), 404
    return jsonify({'post': saved})


@app.route('/archer/posts/<int:post_id>', methods=['DELETE'])
def archer_post_delete(post_id):
    """Hard delete. Use bulk_status with 'archived' for soft delete."""
    import posts as _posts
    if not _posts.delete_post(post_id):
        return jsonify({'error': 'post not found'}), 404
    return jsonify({'ok': True})


@app.route('/archer/posts/bulk', methods=['POST'])
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


# ── CAMPAIGN BUILDER v3 (Branch 3) ───────────────────────────────────────────
@app.route('/archer/campaigns')
def archer_campaigns_page():
    """Bulk Campaign Builder page — picks N targets, generates N packages."""
    return render_template('archer_campaigns.html')


@app.route('/archer/campaigns/list', methods=['GET'])
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
                   VALUES (?, 'new_campaign', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')""",
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
            campaign_id = cur.lastrowid
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
           VALUES (?, 'boost_post', 'post', ?, ?, ?, ?, ?, 'draft', 1)""",
        (
            creator_id, target_value, brand_slug, product_slug,
            json.dumps(pkg), meta_post_id,
        ),
    )
    conn.commit()
    campaign_id = cur.lastrowid
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

@app.route('/archer/image_proxy')
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

@app.route('/archer/ads')
def archer_ads():
    return render_template('archer_ads.html')

@app.route('/archer/organic')
def archer_organic():
    return render_template('organic_posts.html')


# ── INSIGHTS: clicks × earnings × paid attribution ──────────────────────────
@app.route('/insights')
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


# ── ADMIN: creator management ────────────────────────────────────────────────
@app.route('/admin/creators', methods=['GET'])
def admin_creators():
    """List creators + edit/create form. Hidden URL — no auth in v1."""
    creators = db_schema.list_creators()
    return render_template('admin_creators.html', creators=creators)


@app.route('/admin/creators', methods=['POST'])
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
def admin_creator_get(creator_id):
    creator = db_schema.get_creator(creator_id)
    return jsonify({'creator': creator})

@app.route('/archer/generate_ad_copy', methods=['POST'])
def archer_generate_ad_copy():
    from product_api import ArcherAPI
    data = request.get_json() or {}
    products = data.get('products', '')
    campaign_type = data.get('campaign_type', 'organic Facebook post')
    routing = data.get('routing', 'a shoppable landing page')
    slug = data.get('slug', '')
    product_asins = data.get('product_asins', [])

    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=800,
            system=STEPH_AD_COPY_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Write 3 ad copy variants for a {campaign_type} linking to {routing}. Products: {products}"
            }]
        )

        raw = message.content[0].text.strip().replace('```json', '').replace('```', '').strip()
        parsed = json.loads(raw)
        variants = parsed.get('variants', [])

        # Generate Archer attribution links and wrap in URLGenius (Task 7)
        a = ArcherAPI()
        asin = product_asins[0] if product_asins else None
        from datetime import datetime as _dt
        mmdd = _dt.now().strftime('%m%d')
        var_labels = ['a', 'b', 'c']
        for i, v in enumerate(variants):
            var_letter = var_labels[i] if i < len(var_labels) else str(i)
            label = f"steph-{slug}-var{var_letter}"
            if asin:
                link = a.generate_link(asin, label=label)
                if link:
                    archer_url = link.get('attribution_link') or link.get('url') or ''
                    v['archer_url'] = archer_url
                    v['attribution_url'] = archer_url  # backwards-compat
                    v['label'] = label
                    # Wrap in URLGenius
                    campaign = f"{slug[:10]}-{asin.lower()[:6]}-{mmdd}"
                    ug_result = _make_smart_link(
                        asin=asin, network='archer',
                        utm_source='fb-ad', utm_medium='dark',
                        utm_campaign=campaign,
                        utm_term=f'var{var_letter}',
                    )
                    v['genius_url'] = ug_result['genius_url']

        return jsonify({'variants': variants})

    except Exception as e:
        logging.error(f"[ADS] Ad copy generation failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/archer/ads/save', methods=['POST'])
def archer_save_campaign():
    from product_api import ArcherAPI
    data = request.get_json() or {}
    slug = data.get('slug', '').strip()
    if not slug:
        return jsonify({'error': 'slug required'}), 400

    a = ArcherAPI()
    products = data.get('products', [])
    variants = data.get('variants', [])
    for i, v in enumerate(variants):
        if not v.get('attribution_url') and products:
            asin = products[0].get('asin', '')
            if asin:
                label = f"steph-{slug}-var{['a','b','c'][i]}-{asin.lower()}"
                link = a.generate_link(asin, label=label)
                if link:
                    v['attribution_url'] = link.get('attribution_link') or link.get('url') or ''
                    v['label'] = label

    conn = a._db_connect()
    conn.execute("""
        INSERT OR REPLACE INTO campaigns
        (slug, campaign_type, routing, products_json, variants_json, spend_budget, forecast_roas, status, created_at)
        VALUES (?,?,?,?,?,?,?,'draft',CURRENT_TIMESTAMP)
    """, (
        slug,
        data.get('campaign_type', 'organic'),
        data.get('routing', 'landing'),
        json.dumps(products),
        json.dumps(variants),
        data.get('spend_budget', 0),
        data.get('forecast_roas', '')
    ))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'slug': slug})

@app.route('/archer/ads/campaigns')
def archer_list_campaigns():
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT slug, campaign_type, routing, products_json, forecast_roas, status, created_at FROM campaigns ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    campaigns = []
    for r in rows:
        products = json.loads(r['products_json'] or '[]')
        campaigns.append({
            'slug': r['slug'],
            'campaign_type': r['campaign_type'],
            'routing': r['routing'],
            'product_count': len(products),
            'forecast_roas': r['forecast_roas'] or '—',
            'status': r['status'] or 'draft',
            'created_at': r['created_at'][:10] if r['created_at'] else ''
        })
    return jsonify({'campaigns': campaigns})

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
            'network': network,
            'label': link_label,
            'utm': utm_meta,
            'urlgenius': True,
            'from_registry': ug_result.get('_from_registry', False),
            'link_id': link_obj.get('id') if isinstance(link_obj, dict) else None,
        })
    except Exception as e:
        logging.error(f"[URLGENIUS] smart_link failed: {e}")
        return jsonify({
            'genius_url': affiliate_url,
            'affiliate_url': affiliate_url,
            'network': network,
            'label': link_label,
            'utm': utm_meta,
            'urlgenius': False,
        })


# ARCHIVED — see /archive/routes/




@app.route('/archer/discovery/top_clicked', methods=['GET'])
def archer_discovery_top_clicked():
    """Top URLGenius-clicked Amazon products for Organic queue seeding."""
    from product_api import URLGeniusAPI, ArcherAPI
    import re

    def _asin_from_text(*vals):
        pat = re.compile(r'(?:/dp/|/gp/product/|/product/)([A-Z0-9]{10})(?:[/?&#]|$)', re.I)
        for v in vals:
            txt = str(v or '')
            m = pat.search(txt)
            if m:
                return m.group(1).upper()
        return None

    min_clicks = int(request.args.get('min_clicks', 300))
    limit = max(1, min(int(request.args.get('limit', 35)), 60))
    seed_limit = max(100, min(int(request.args.get('seed_limit', 500)), 1000))

    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'error': 'URLGENIUS_API_KEY not set'}), 400

    # URLgenius API v2 has no list endpoint; we always serve from the local
    # registry. "registry_only" signals to the frontend that click_30d data
    # may not be present for every link.
    raw = ug.list_links(limit=seed_limit)
    links = raw.get('links', [])
    registry_only = True

    scored = {}
    for lk in links:
        asin = _asin_from_text(
            lk.get('url'), lk.get('destination_url'), lk.get('affiliate_url'),
            lk.get('genius_url'), lk.get('long_url'), lk.get('deeplink'),
        )
        if not asin:
            continue

        clicks = (
            lk.get('clicks_30d') or lk.get('clicks30d') or lk.get('clicks')
            or (lk.get('stats') or {}).get('clicks_30d')
            or (lk.get('stats') or {}).get('clicks')
            or (lk.get('metrics') or {}).get('clicks_30d')
            or (lk.get('metrics') or {}).get('clicks')
            or 0
        )
        try:
            clicks = int(float(clicks))
        except Exception:
            clicks = 0

        prev = scored.get(asin)
        if prev is None or clicks > prev['clicks']:
            scored[asin] = {'clicks': clicks, 'source': lk}

    # If no link in the registry has any recorded click data, drop the
    # threshold so we still surface candidate products. (URLgenius API v2
    # doesn't expose click counts via any documented endpoint.)
    has_any_clicks = any(v['clicks'] > 0 for v in scored.values())
    effective_min_clicks = min_clicks if has_any_clicks else 0
    picked = [(a, v) for a, v in scored.items() if v['clicks'] >= effective_min_clicks]
    picked.sort(key=lambda t: t[1]['clicks'], reverse=True)
    picked = picked[:limit]

    asins = [a for a, _ in picked]
    catalog = ArcherAPI().get_by_asins(asins) if asins else []
    by_asin = {(p.get('asin') or '').upper(): p for p in catalog}

    out = []
    for asin, meta in picked:
        p = by_asin.get(asin, {})
        lk = meta['source']
        out.append({
            'asin': asin,
            'clicks_30d': meta['clicks'],
            'product_name': p.get('product_name') or p.get('name') or lk.get('title') or asin,
            'company_name': p.get('company_name') or p.get('brand') or '',
            'price': p.get('price') or '',
            'commission_payout': p.get('commission_payout') or p.get('commission') or '',
            'image_encoded_string': p.get('image_encoded_string') or '',
            'urlgenius_url': lk.get('genius_url') or '',
            'destination_url': lk.get('url') or lk.get('destination_url') or '',
        })

    return jsonify({
        'products': out,
        'count': len(out),
        'filters': {
            'min_clicks': min_clicks,
            'effective_min_clicks': effective_min_clicks,
            'limit': limit,
        },
        'registry_only': registry_only,
        'has_click_data': has_any_clicks,
    })
@app.route('/urlgenius/create_link', methods=['POST'])
def urlgenius_create_link():
    from product_api import URLGeniusAPI
    body = request.get_json() or {}
    url = body.get('url', '').strip()
    if not url:
        return jsonify({'error': 'url is required'}), 400
    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'error': 'URLGENIUS_API_KEY not set'}), 400
    try:
        result = ug.create_link(
            destination_url=url,
            utm_source=body.get('utm_source', 'steph-ai'),
            utm_medium=body.get('utm_medium', 'ai-agent'),
            utm_campaign=body.get('utm_campaign'),
            utm_content=body.get('utm_content'),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/urlgenius')
@app.route('/archer/urlgenius')
def urlgenius_page():
    return render_template('urlgenius_links.html')


@app.route('/urlgenius/sync', methods=['POST'])
def urlgenius_sync_registry():
    """
    Reload the local URLgenius link registry from disk.

    URLgenius API v2 has no documented list endpoint, so the registry is
    the source of truth. This is a fast read-only operation and does not
    require an API key.
    """
    from product_api import URLGeniusAPI
    ug = URLGeniusAPI()
    started = time.time()
    try:
        n = ug.seed_registry()
        return jsonify({
            'ok': True,
            'seeded_links': n,
            'duration_ms': int((time.time() - started) * 1000),
        })
    except Exception as e:
        logging.warning(f"[URLGENIUS] manual sync failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/urlgenius/links')
def urlgenius_list_links():
    """
    Return URLgenius deep links from the local registry.

    URLgenius API v2 (per official docs) only supports POST (create) and
    DELETE — there is no documented endpoint to list links — so the local
    registry, populated as we create links, is the authoritative source.
    No API key required since this reads from local disk.
    """
    from product_api import URLGeniusAPI
    ug = URLGeniusAPI()
    try:
        limit = int(request.args.get('limit', 500))
    except (TypeError, ValueError):
        limit = 500
    limit = max(1, min(limit, 5000))
    return jsonify(ug.list_links(limit=limit))


# ── LEVANTA ───────────────────────────────────────────────────────────────────

@app.route('/levanta/generate_link', methods=['POST'])
def levanta_generate_link():
    from product_api import LevantaAPI
    data = request.get_json() or {}
    asin = data.get('asin', '').strip()
    label = data.get('label', asin)
    if not asin:
        return jsonify({'error': 'asin is required'}), 400
    lv = LevantaAPI()
    try:
        result = lv.create_product_link(asin, source_id=label)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/levanta/deals')
def levanta_deals():
    from product_api import LevantaAPI
    lv = LevantaAPI()
    try:
        return jsonify(lv.get_deals())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/webhooks/levanta', methods=['POST'])
def levanta_webhook():
    """Receive real-time Levanta events."""
    import hmac as hmac_lib, hashlib
    secret = os.environ.get('LEVANTA_WEBHOOK_SECRET', '')
    sig_header = request.headers.get('x-levanta-hmac-sha256', '')
    if secret:
        expected = hmac_lib.new(
            secret.encode(), request.get_data(), hashlib.sha256
        ).hexdigest()
        if not hmac_lib.compare_digest(expected, sig_header):
            return jsonify({'error': 'Invalid signature'}), 401

    event = request.get_json() or {}
    event_type = event.get('type', '')
    data = event.get('data', {})
    logging.info(f"[LEVANTA WEBHOOK] Event: {event_type} | Data: {data}")

    if event_type == 'product.access.gained':
        asin = data.get('asin')
        logging.info(f"[LEVANTA] New product access: {asin} at {data.get('commission', 0) * 100:.0f}%")
    elif event_type == 'link.disabled':
        logging.warning(f"[LEVANTA] Link disabled: {data.get('id')}")
    elif event_type == 'product.added':
        logging.info(f"[LEVANTA] New product in catalog: {data.get('asin')}")
    elif event_type == 'product.removed':
        logging.warning(f"[LEVANTA] Product removed: {data.get('asin')}")

    return jsonify({'received': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
