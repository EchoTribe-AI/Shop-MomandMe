import os
import re
import json
import logging
import sqlite3
import time
import tempfile
import requests as req
from flask import Flask, send_from_directory, request, jsonify, render_template, Response, redirect, url_for
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

import storefront_chat
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
    'sage':     {'bg': '#f0f4f0', 'accent': '#3a7a4a', 'btn': '#2d6b3c', 'text': '#1a1a17'},
    'sand':     {'bg': '#fdf8f0', 'accent': '#8a6a3a', 'btn': '#7a5a2a', 'text': '#1a1a17'},
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


@app.route('/hub')
def hub():
    return render_template('hub.html', shop_subdomain=SHOP_SUBDOMAIN)

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
            utm_defaults = data.get('utm_defaults') or {}
            persisted = []
            asin_to_product = {(p.get('asin') or '').strip(): p for p in product_list}
            for raw_post in parsed.get('posts', []):
                asin = (raw_post.get('asin') or '').strip()
                product = asin_to_product.get(asin, {})
                product_network = str(
                    product.get('network')
                    or product.get('retailer')
                    or product.get('retailer_name')
                    or 'amazon'
                ).strip().lower()
                if product_network == 'walmart':
                    product_network = 'walmart'
                angle = raw_post.get('angle', '')
                utm = _organic_static_utm(product, asin, angle, utm_defaults)
                smart = {
                    'genius_url': '',
                    'affiliate_url': '',
                    'final_url': '',
                    'link_id': '',
                }
                enriched_post_fields = {}
                if product_network == 'walmart':
                    try:
                        import walmart_storefront_enrichment as _walmart_enrichment
                        enriched_post_fields = _walmart_enrichment.post_update_fields({
                            'asin': asin,
                            'network': 'walmart',
                            'product_name': product.get('product_name') or product.get('name') or '',
                            'product_brand': product.get('brand') or product.get('company_name') or '',
                            'product_price': product.get('price_display') or product.get('price') or '',
                            'product_image': product.get('image_encoded_string') or product.get('image_url') or '',
                        })
                    except Exception as _e:
                        logging.warning(f"[GENERATE_POSTS] Walmart enrichment failed for {asin}: {_e}")
                    smart['genius_url'] = (
                        product.get('smart_link')
                        or product.get('attribution_link')
                        or product.get('shop_url')
                        or product.get('url')
                        or ''
                    )
                elif asin and not collection_slug:
                    try:
                        smart = _amazon_urlgenius_link(asin, utm)
                    except Exception as _e:
                        logging.warning(f"[GENERATE_POSTS] URLGenius link failed for {asin}: {_e}")
                        smart['genius_url'] = f"https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}"
                        smart['affiliate_url'] = smart['genius_url']
                try:
                    saved = _posts.create_post(
                        creator_id=creator_id,
                        asin=asin,
                        angle=angle,
                        copy=raw_post.get('copy', ''),
                        image_note=raw_post.get('image_note', ''),
                        network=product_network,
                        collection_slug=collection_slug,
                        status='draft',
                        utm=utm,
                        smart_link=smart.get('genius_url') or '',
                        smart_link_id=smart.get('link_id') or '',
                        smart_link_affiliate_url=smart.get('affiliate_url') or '',
                        smart_link_final_url=smart.get('final_url') or '',
                        product_name=enriched_post_fields.get('product_name') or product.get('product_name') or product.get('name') or '',
                        product_brand=enriched_post_fields.get('product_brand') or product.get('brand') or product.get('company_name') or '',
                        product_price=enriched_post_fields.get('product_price') or product.get('price_display') or product.get('price') or '',
                        product_image=enriched_post_fields.get('product_image') or product.get('image_encoded_string') or product.get('image_url') or '',
                        product_availability=enriched_post_fields.get('product_availability') or '',
                        product_rating=enriched_post_fields.get('product_rating'),
                        product_review_count=enriched_post_fields.get('product_review_count'),
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
def archer_collage_publish():
    """Promote a draft collage to published. Generates Archer attribution links
    for products that don't have them yet, then flips status to 'published'.

    Body: { "slug": "..." }
    """
    import collection_service
    from product_api import ArcherAPI
    data = request.get_json() or {}
    slug = data.get('slug')
    archer = None

    def generate_link(asin, label):
        nonlocal archer
        if archer is None:
            archer = ArcherAPI()
        return archer.generate_link(asin, label=label)

    try:
        result = collection_service.publish_collage(
            slug,
            shop_subdomain=SHOP_SUBDOMAIN,
            link_generator=generate_link,
        )
        return jsonify(result)
    except collection_service.CollectionServiceError as exc:
        status_code = 404 if str(exc) == 'collection not found' else 400
        return jsonify({'error': str(exc)}), status_code

@app.route('/archer/collage/archive', methods=['POST'])
def archer_collage_archive():
    """Soft-delete a collage by setting its status to 'archived'.

    Bypasses the trend-origin save guard intentionally — archiving is always
    allowed regardless of how the page originated.

    Body: { "slug": "..." }
    """
    import collection_service
    data = request.get_json() or {}
    slug = (data.get('slug') or '').strip()
    if not slug:
        return jsonify({'error': 'slug is required'}), 400
    clean_slug = collection_service.normalize_slug(slug)
    conn = collection_service._connect()
    try:
        row = conn.execute("SELECT slug FROM collages WHERE slug = ?", (clean_slug,)).fetchone()
        if not row:
            return jsonify({'error': 'collection not found'}), 404
        conn.execute("UPDATE collages SET status = 'archived' WHERE slug = ?", (clean_slug,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'slug': clean_slug, 'status': 'archived'})


@app.route('/archer/collage/<slug>', methods=['GET'])
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
    """Protect Walmart trend mutation endpoints with an env-backed token.

    Existing admin pages in this app are hidden but not authenticated, so these
    API mutation routes use a stricter Replit Secret guard. Send the token as
    `X-Walmart-Trends-Admin-Token` or `Authorization: Bearer <token>`.
    """
    expected = (
        os.environ.get('WALMART_TRENDS_ADMIN_TOKEN')
        or os.environ.get('ADMIN_API_TOKEN')
        or os.environ.get('ADMIN_SECRET')
    )
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
    """Allow demo mutations without a secret only in local/Replit dev contexts."""
    host = (request.host or '').split(':')[0].lower()
    return bool(
        os.environ.get('FLASK_ENV') == 'development'
        or os.environ.get('FLASK_DEBUG') == '1'
        or os.environ.get('REPLIT_DEV_DOMAIN')
        or host in {'localhost', '127.0.0.1'}
        or host.endswith('.replit.dev')
        or host.endswith('.repl.co')
    )


def _require_walmart_admin_if_configured():
    """Protect content mutations while allowing explicit local/Replit demo mode."""
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
            'message': 'Admin token required. Open the page with ?admin_token=<token> or enable demo mode in a local/Replit dev environment.',
        }), 401
    if _walmart_content_demo_allowed():
        return None
    return jsonify({'error': 'admin token is not configured for this production environment'}), 503


@app.route('/walmart/trending-now')
def walmart_trending_now_page():
    """Mobile-first Walmart What's Trending Now landing page."""
    from walmart_trends import get_trending_page_data, discover_workbooks

    data = get_trending_page_data()
    admin_mode = request.args.get('admin') == '1'
    admin_token = (request.args.get('admin_token') or '').strip()
    workbooks = discover_workbooks() if admin_mode else []
    shop_nav_items = _public_shop_nav('trends')
    if admin_mode:
        shop_nav_items = [item for item in shop_nav_items if item['key'] == 'trends']
    return render_template(
        'walmart_trending_now.html',
        data=data,
        admin_mode=admin_mode,
        admin_token=admin_token,
        workbooks=workbooks,
        shop_subdomain=SHOP_SUBDOMAIN,
        public_nav_items=shop_nav_items,
        nav_active='trends',
    )


@app.route('/trends')
def shop_trends():
    """Public Walmart trends home for the shop subdomain/menu."""
    from walmart_trends import get_trending_page_data
    try:
        data = get_trending_page_data()
    except Exception as exc:
        logging.warning("[WALMART_TRENDS] public trends unavailable: %s", exc)
        data = {'last_refreshed': '', 'collections': []}

    return render_template(
        'walmart_trending_now.html',
        data=data,
        admin_mode=False,
        admin_token='',
        shop_subdomain=SHOP_SUBDOMAIN,
        public_nav_items=_public_shop_nav('trends'),
        nav_active='trends',
    )


@app.route('/api/walmart/trending-now')
def walmart_trending_now_api():
    """JSON source for the Walmart What's Trending Now page."""
    from walmart_trends import get_trending_page_data

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
    import collection_content as cc

    collection = cc.get_walmart_collection(collection_slug)
    if not collection:
        return "Collection not found", 404
    creator_id = (request.args.get('creator_id') or 'everydaywithsteph').strip()
    admin_token = (request.args.get('admin_token') or '').strip()
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
        admin_token=admin_token,
        demo_auth_allowed=_walmart_content_demo_allowed(),
        existing_draft=existing_draft,
        editor_mode='create',
        shop_subdomain=SHOP_SUBDOMAIN,
        retailer_ctx=rctx,
    )


def _render_collection_page_edit(public_slug):
    """Shared handler for the (retailer-agnostic) page editor."""
    import collection_content as cc

    draft = cc.get_latest_draft_for_public_slug(public_slug)
    if not draft:
        return "Page draft not found", 404
    collection_slug = draft.get('source_collection_slug') or ''
    collection = cc.get_walmart_collection(collection_slug) if collection_slug else None
    if not collection:
        collection = cc.collection_from_draft_snapshot(draft)
    creator_id = (request.args.get('creator_id') or draft.get('creator_id') or 'everydaywithsteph').strip()
    admin_token = (request.args.get('admin_token') or '').strip()
    products = draft.get('product_snapshot') or collection.get('items', []) or []
    rctx = _editor_retailer_context(collection)
    return render_template(
        'walmart_collection_create_post.html',
        collection=collection,
        products=products,
        product_count=len(products),
        creator_id=creator_id,
        default_public_slug=draft.get('public_slug') or public_slug,
        admin_token=admin_token,
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
    return _render_collection_create_post(collection_slug)


@app.route('/collections/<public_slug>/edit')
def collection_page_edit(public_slug):
    """Canonical editor for a published collection page."""
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

    if not cc.get_walmart_collection(collection_slug):
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
            'collection_slug': collection_slug,
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
        generated = cc.generate_walmart_collection_content(
            collection_slug=collection_slug,
            creator_id=(body.get('creator_id') or 'everydaywithsteph').strip(),
            voice_source_text=body.get('voice_source_text') or '',
            platform=body.get('platform') or 'facebook_group',
            tone=body.get('tone') or 'warm mom-to-mom',
            audience_context=body.get('audience_context') or 'busy moms looking for timely creator finds',
            allow_demo_fallback=False,
            regenerate_target=body.get('regenerate_target') or '',
        )
        draft_id = body.get('draft_id')
        response = {'source_type': cc.SOURCE_WALMART_TREND, 'source_collection_slug': collection_slug, **generated}
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
        draft = cc.save_walmart_collection_draft(collection_slug, body, status='draft')
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
    for slug, updated in rows:
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


@app.route('/archer/posts/manage', methods=['GET'])
def archer_posts_manage_page():
    """Dedicated operations page for saved organic posts and collections."""
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
    import posts as _posts
    post = _posts.get_post(post_id)
    if not post:
        return "Post not found", 404
    return render_template('organic_post_edit.html', post=post, amazon_tag=AMAZON_TAG)


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
    post_id = request.args.get('post_id')
    if post_id and post_id.isdigit():
        return redirect(url_for('archer_post_edit_page', post_id=int(post_id)))
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
        INSERT INTO campaigns
        (slug, campaign_type, routing, products_json, variants_json, spend_budget, forecast_roas, status, created_at)
        VALUES (?,?,?,?,?,?,?,'draft',CURRENT_TIMESTAMP)
        ON CONFLICT (slug) DO UPDATE SET
          campaign_type=EXCLUDED.campaign_type, routing=EXCLUDED.routing,
          products_json=EXCLUDED.products_json, variants_json=EXCLUDED.variants_json,
          spend_budget=EXCLUDED.spend_budget, forecast_roas=EXCLUDED.forecast_roas
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
            'created_at': _fmt_date(r['created_at'])
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
    # How many of the highest-click links to scan for ASINs. Default 5000
    # is plenty since most non-Amazon entries (Walmart/Target) won't yield
    # an ASIN anyway — this just bounds CPU work, not data coverage.
    seed_limit = max(100, min(int(request.args.get('seed_limit', 5000)), 30000))

    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'error': 'URLGENIUS_API_KEY not set'}), 400

    # IMPORTANT: load the FULL registry first, then sort by clicks desc,
    # then truncate. Otherwise we'd only sort within an arbitrary insertion-
    # order slice and miss high-click rows scattered throughout the file.
    raw = ug.list_links(limit=30000)
    links = raw.get('links', [])
    links.sort(
        key=lambda l: int(l.get('clicks') or l.get('clicks_30d') or 0),
        reverse=True,
    )
    links = links[:seed_limit]
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
    Refresh live click counts for registry entries via the documented
    URLgenius v2 endpoint: GET /api/v2/links/<LINK_ID>.

    Body params (all optional):
      - limit: how many links to refresh in this call (1-500, default 50).
               Each call costs ~limit*0.55s due to the 2-req/sec API limit,
               so syncing all ~20K links happens progressively over many
               clicks (oldest-checked first).
      - all:   if true, ignores the 24h "stale" filter and re-checks even
               recently-synced rows.
    """
    from product_api import URLGeniusAPI
    body = request.get_json(silent=True) or {}
    limit = body.get('limit') or request.args.get('limit', 50)
    only_stale = not (body.get('all') or request.args.get('all') == '1')

    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'ok': False, 'error': 'URLGENIUS_API_KEY not set'}), 400
    try:
        result = ug.refresh_link_clicks(limit=limit, only_stale=only_stale)
        result['ok'] = True
        return jsonify(result)
    except Exception as e:
        logging.warning(f"[URLGENIUS] sync failed: {e}")
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
    # Registry now holds 21K+ entries — allow loading the full set so the
    # dashboard can show real totals/sort across the whole catalog.
    limit = max(1, min(limit, 30000))
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
