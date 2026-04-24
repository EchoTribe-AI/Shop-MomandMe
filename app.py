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
from product_api import ProductResolver, detect_category
from prompts import (
    build_chat_prompt, build_chat_products,
    STEPH_CAPTION_PROMPT, STEPH_AD_COPY_PROMPT,
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

@app.route('/')
def index():
    return render_template('dashboard.html')

# ARCHIVED — see /archive/routes/

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/dashboard/upload_csv', methods=['POST'])
def dashboard_upload_csv():
    import csv, io
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.csv'):
        return jsonify({'error': 'File must be a .csv'}), 400
    try:
        text = f.read().decode('utf-8-sig')
        reader_io = io.StringIO(text)
        next(reader_io)  # skip report title row
        rows = list(csv.DictReader(reader_io))
        products = []
        for row in rows:
            asin = (row.get('ASIN') or '').strip()
            if not asin:
                continue
            try:
                earnings = float(row.get('Total Earnings') or row.get('Revenue($)') or 0)
                units    = int(float(row.get('Items Shipped') or 0))
            except (ValueError, TypeError):
                earnings, units = 0.0, 0
            products.append({
                'asin':           asin,
                'product_name':   (row.get('Name') or row.get('Title') or asin).strip(),
                'total_earnings': round(earnings, 2),
                'items_shipped':  units,
            })
        products.sort(key=lambda p: p['total_earnings'], reverse=True)
        return jsonify({'products': products[:10]})
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

@app.route('/archer/collage/save', methods=['POST'])
def archer_save_collage():
    from product_api import ArcherAPI
    data = request.get_json() or {}
    slug = data.get('slug', '').strip().lower().replace(' ', '-')
    if not slug or not data.get('products'):
        return jsonify({'error': 'slug and products required'}), 400

    a = ArcherAPI()
    products = data.get('products', [])
    for p in products:
        asin = p.get('asin', '')
        if asin and not p.get('attribution_link'):
            link = a.generate_link(asin, label=f"{slug}-{asin.lower()}")
            if link:
                p['attribution_link'] = link.get('attribution_link') or link.get('url') or ''

    conn = a._db_connect()
    conn.execute("""
        INSERT OR REPLACE INTO collages
        (slug, products_json, layout, theme, caption, direct_to_amazon, created_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        slug,
        json.dumps(products),
        data.get('layout', 'layout-2'),
        data.get('theme', 'coral'),
        data.get('caption', ''),
        1 if data.get('direct_to_amazon') else 0
    ))
    conn.commit()
    conn.close()
    return jsonify({'url': f'/shop/{slug}', 'slug': slug})

@app.route('/archer/collages')
def archer_list_collages():
    from product_api import ArcherAPI
    a = ArcherAPI()
    conn = a._db_connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT slug, theme, layout, created_at, click_count, products_json FROM collages ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    collages = []
    for r in rows:
        products = json.loads(r['products_json'] or '[]')
        collages.append({
            'slug': r['slug'],
            'theme': r['theme'],
            'layout': r['layout'],
            'created_at': r['created_at'][:10] if r['created_at'] else '',
            'click_count': r['click_count'] or 0,
            'product_count': len(products)
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
    products = json.loads(collage.get('products_json') or '[]')
    collage['direct_to_amazon'] = bool(collage.get('direct_to_amazon'))
    return render_template('shop_landing.html',
        collage=collage,
        products=products,
        themes=THEMES,
        pixel_id=PIXEL_ID
    )

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

# Valid source × medium combinations (Task 5 — naming convention)
# channel = utm_source | type = utm_medium
VALID_PLACEMENTS = {
    'fb-group': ['organic'],
    'fb-ad':    ['dark', 'boost'],
    # Legacy — kept for backwards-compat with existing saved links
    'facebook':  ['organic', 'paid'],
    'instagram': ['organic', 'paid'],
    'tiktok':    ['organic', 'paid'],
    'email':     ['newsletter'],
    'steph-ai':  ['ai-agent'],
}

# utm_content auto-derived from affiliate network (never supplied by caller)
NETWORK_CONTENT = {
    'amazon':  'amazon-assoc',
    'archer':  'archer',
    'levanta': 'levanta',
}

# Seed URLGenius registry on startup
def _seed_urlgenius():
    try:
        from product_api import URLGeniusAPI
        ug = URLGeniusAPI()
        if ug.api_key:
            n = ug.seed_registry()
            logging.info(f"[URLGENIUS] Startup seed: {n} links loaded")
    except Exception as e:
        logging.warning(f"[URLGENIUS] Startup seed failed: {e}")

_seed_urlgenius()


def _make_smart_link(asin: str, network: str = 'amazon', utm_source: str = 'fb-group',
                     utm_medium: str = 'organic', utm_campaign: str = '',
                     utm_term: str = '') -> dict:
    """
    Internal helper: build an affiliate URL and wrap it in URLGenius.
    Respects 2 req/sec URLGenius limit via 500ms sleep before each API call.
    Returns dict with genius_url, affiliate_url, label, urlgenius (bool).
    """
    from product_api import ArcherAPI, URLGeniusAPI
    from datetime import datetime as _dt

    amazon_tag = os.environ.get('AMAZON_AFFILIATE_TAG', 'mommymedeals-20')
    affiliate_url = f'https://www.amazon.com/dp/{asin}?tag={amazon_tag}'

    if network == 'archer':
        try:
            a = ArcherAPI()
            label_archer = f'steph-archer-{asin.lower()}-{int(time.time())}'
            result = a.generate_link(asin, label=label_archer)
            if result:
                affiliate_url = (result.get('attribution_link') or result.get('url')
                                 or result.get('link') or affiliate_url)
        except Exception as e:
            logging.warning(f'[SMART_LINK] Archer link failed for {asin}: {e}')

    mmdd = _dt.now().strftime('%m%d')
    link_label = f'{utm_source}_{utm_medium}_{utm_campaign}_{mmdd}'
    utm_content = NETWORK_CONTENT.get(network, network)

    ug = URLGeniusAPI()
    if not ug.api_key:
        return {'genius_url': affiliate_url, 'affiliate_url': affiliate_url,
                'label': link_label, 'urlgenius': False}
    try:
        time.sleep(0.5)  # 2 req/sec URLGenius rate limit
        ug_result = ug.create_link(
            destination_url=affiliate_url,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            utm_content=utm_content,
            utm_term=utm_term or None,
        )
        link_obj = ug_result.get('link', {})
        if isinstance(link_obj, dict) and link_obj.get('genius_url'):
            genius_url = link_obj['genius_url']
        else:
            genius_url = (link_obj.get('genius_url', affiliate_url)
                          if isinstance(link_obj, dict) else affiliate_url)
        return {'genius_url': genius_url, 'affiliate_url': affiliate_url,
                'label': link_label, 'urlgenius': True}
    except Exception as e:
        logging.warning(f'[SMART_LINK] URLGenius call failed for {asin}: {e}')
        return {'genius_url': affiliate_url, 'affiliate_url': affiliate_url,
                'label': link_label, 'urlgenius': False}


@app.route('/urlgenius/smart_link', methods=['POST'])
def urlgenius_smart_link():
    """
    Generate a URLGenius deep link for a product using the full UTM attribution schema.
    Body: {
      asin: str,
      network: 'amazon' | 'archer' | 'levanta',
      placement: { source, medium, campaign, term? },
      force_new?: bool
    }
    utm_content is derived automatically from network — never supplied by caller.
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

    # ── utm_content auto-derived from network ───────────────────────────────
    utm_content = NETWORK_CONTENT.get(network, network)

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


@app.route('/urlgenius/links')
def urlgenius_list_links():
    from product_api import URLGeniusAPI
    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'error': 'URLGENIUS_API_KEY not set'}), 400
    try:
        return jsonify(ug.list_links())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
