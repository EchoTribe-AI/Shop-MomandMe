import os
import json
import logging
import sqlite3
import requests as req
from flask import Flask, send_from_directory, request, jsonify, render_template, Response
from dotenv import load_dotenv
import anthropic
from product_api import ProductResolver, detect_category

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

# Product catalog matching the frontend
PRODUCTS = [
    {'id': 0, 'name': 'Barbie Dreamhouse Pool Party 75+ Pieces', 'price': '$179', 'was': '$210', 'retailer': 'Amazon', 'emoji': '🏠', 'link': 'https://amazon.com/dp/B0C...?tag=mommymedeals-20'},
    {'id': 1, 'name': '2026 Glitter Dumpling Squishy Toy', 'price': '$13.49', 'was': '', 'retailer': 'Amazon', 'emoji': '✨', 'link': 'https://amazon.com/dp/B0D...?tag=mommymedeals-20'},
    {'id': 2, 'name': 'Ms. Rachel Toddler Hoodie + Jogger Set', 'price': '$7.00', 'was': '$15.98', 'retailer': 'Walmart', 'emoji': '🧸', 'link': 'https://goto.walmart.com/ZVboz1'},
    {'id': 3, 'name': 'Melissa & Doug Steering Wheel Dashboard', 'price': '$28', 'was': '', 'retailer': 'Amazon', 'emoji': '🚗', 'link': 'https://amazon.com/dp/B0A...?tag=mommymedeals-20'},
    {'id': 4, 'name': 'Stanley Quencher 40oz Tumbler', 'price': '$35', 'was': '$45', 'retailer': 'Amazon', 'emoji': '🥤', 'link': 'https://amazon.com/dp/B09...?tag=mommymedeals-20'},
    {'id': 5, 'name': 'Moana 2 Kids Underwear 7-Pack', 'price': '$10', 'was': '', 'retailer': 'Amazon', 'emoji': '🌊', 'link': 'https://amazon.com/dp/B0E...?tag=mommymedeals-20'},
    {'id': 6, 'name': 'Imaginext Jurassic World Dinosaur Set', 'price': '$35', 'was': '$49', 'retailer': 'Walmart', 'emoji': '🦕', 'link': 'https://goto.walmart.com/'},
    {'id': 7, 'name': 'Sol de Janeiro Travel Fragrance Set', 'price': '$32', 'was': '', 'retailer': 'Ulta', 'emoji': '🌸', 'link': 'https://www.ulta.com/...?PID=1390'},
    {'id': 8, 'name': 'Kinetic Sand Deluxe Gift Bag', 'price': '$14', 'was': '', 'retailer': 'Target', 'emoji': '⏳', 'link': 'https://target.com/'},
    {'id': 9, 'name': 'Keter Plastic Storage Box 55-Gallon', 'price': '$39', 'was': '$55', 'retailer': 'Wayfair', 'emoji': '📦', 'link': 'https://wayfair.com/'},
    {'id': 10, 'name': 'OXO Good Grips Silicone Utensil Set', 'price': '$18.99', 'was': '$24.99', 'retailer': 'Walmart', 'emoji': '🍳', 'link': 'https://goto.walmart.com/c/kitchen', 'category': 'home'},
    {'id': 11, 'name': 'Instant Pot Duo Crisp 8-Quart Pressure Cooker', 'price': '$99', 'was': '$149', 'retailer': 'Amazon', 'emoji': '🍲', 'link': 'https://amazon.com/dp/B08...?tag=mommymedeals-20', 'category': 'home'},
    {'id': 12, 'name': 'ChefJet 3-in-1 Vegetable Chopper', 'price': '$16.49', 'was': '$19.99', 'retailer': 'Walmart', 'emoji': '🥬', 'link': 'https://goto.walmart.com/c/kitchen', 'category': 'home'},
]

product_resolver = ProductResolver(PRODUCTS)

SYSTEM_PROMPT = """You are Steph, the creator behind @EverydaywithSteph and the Mommy & Me Collective. You talk mom-to-mom: warm, enthusiastic, concise, and occasionally use light emojis (but not excessively). You share deals and product recommendations like a trusted friend who happens to know every sale happening right now.

Your current top products and data:

PRODUCTS (index by ID for recommendations):
0. Barbie Dreamhouse Pool Party | $179 (was $210) | Amazon | 37,199 clicks | score 94 | category: toys
1. Glitter Dumpling Squishy 2026 | $13.49 | Amazon | 702 units sold | score 89 | category: toys
2. Ms. Rachel Toddler Set | $7.00 (was $15.98) | Walmart | 56% off clearance | score 82 | category: toys
3. Melissa & Doug Dashboard | $28 | Amazon | 262 clicks today | score 78 | category: toys
4. Stanley Quencher 40oz | $35 (was $45) | Amazon | 1,300 clicks | score 68 | category: beauty
5. Moana 2 Underwear 7-Pack | $10 | Amazon | 5,840 clicks | score 72 | category: baby
6. Imaginext Jurassic Dino Set | $35 (was $49) | Walmart | Walmart storefront pick | score 65 | category: toys
7. Sol de Janeiro Travel Set | $32 | Ulta | $270 earned, 42 orders | score 71 | category: beauty
8. Kinetic Sand Gift Bag | $14 | Target | 4,278 clicks | score 63 | category: toys
9. Keter Storage Box | $39 (was $55) | Wayfair | Top Wayfair earner | score 58 | category: home
10. OXO Good Grips Silicone Utensil Set | $18.99 (was $24.99) | Walmart | kitchen essentials | score 76 | category: home
11. Instant Pot Duo Crisp 8-Quart | $99 (was $149) | Amazon | 2,150 clicks | score 85 | category: home
12. ChefJet 3-in-1 Vegetable Chopper | $16.49 (was $19.99) | Walmart | meal prep helper | score 74 | category: home

KEY FACTS:
- Walmart converts at 16.7% — always route budget deals there first
- Toys & Games is your top Amazon category by clicks and revenue
- Barbie Dreamhouse has 37K clicks — your single highest-traffic product
- Your LTK storefront: shopltk.com/EverydaywithSteph

RESPONSE RULES:
- Keep replies to 2-4 sentences max
- Recommend specific products with prices when relevant
- If a budget deal exists at Walmart, mention Walmart first
- End with a helpful nudge when natural
- Never break character or mention Claude/AI

PRODUCT RECOMMENDATION FORMAT (CRITICAL - ALWAYS FOLLOW):
You MUST end EVERY response with either PRODUCTS: or SEARCH: line. Never end without one.

**Option 1: PRODUCTS format** (when you have exact matches in the catalog above)
End with: PRODUCTS: 0,1,2
Example:
User: "best toy under $30?"
Response: "oooh I have the PERFECT picks for you! The Ms. Rachel set is only $7 at Walmart right now (56% off 😱), or the Glitter Dumpling Squishy for $13.49 — my kids are OBSESSED with it. Both are total winners!
PRODUCTS: 2,1"

**Option 2: SEARCH format** (when user asks for something NOT in your catalog)
End with: SEARCH: category searchterm
Example:
User: "show me cheap kitchen gadgets"
Response: "Let me find you some amazing kitchen gadgets that won't break the bank!
SEARCH: home kitchen gadgets cheap"

User: "what about bluetooth speakers?"
Response: "Great question! Let me search for those!
SEARCH: electronics bluetooth speakers"

RULES:
- If your 10 Hot Score products match the user's request → use PRODUCTS: format
- If user asks for something OUTSIDE your catalog → ALWAYS use SEARCH: format
- Kitchen gadgets → NOT in catalog → use SEARCH:
- Bluetooth speakers → NOT in catalog → use SEARCH:
- Toys under $30 → IN catalog → use PRODUCTS:
- DO NOT respond without a final PRODUCTS: or SEARCH: line
- SEARCH: queries should be concise (2-3 keywords max)"""

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    print(f"[CHAT] Received message: {user_message[:50]}...")
    if not user_message:
        return jsonify({'error': 'message is required'}), 400

    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=256,
            system=SYSTEM_PROMPT,
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
                products = [PRODUCTS[pid] for pid in product_ids if 0 <= pid < len(PRODUCTS)]
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
    return send_from_directory('.', 'index.html')

@app.route('/plan')
def plan():
    return send_from_directory('.', 'steph-ai-plan.html')

@app.route('/architecture')
def architecture():
    return send_from_directory('.', 'steph-architecture.html')

@app.route('/connections')
def connections():
    return send_from_directory('.', 'steph-connection-map.html')

@app.route('/archer/products')
def archer_products():
    return render_template('archer_products.html')

@app.route('/archer/matched')
def archer_matched():
    """Return matched ASINs from matched_asins.json with pagination."""
    from product_api import ArcherAPI
    a = ArcherAPI()
    limit  = min(int(request.args.get('limit', 20)), 100)
    offset = int(request.args.get('offset', 0))
    all_products = a._load_matched_json()
    total = len(all_products)
    page  = all_products[offset:offset + limit]
    return jsonify({'products': page, 'total': total, 'has_more': offset + limit < total})


@app.route('/archer/upload_earnings', methods=['POST'])
def archer_upload_earnings():
    """
    Accept an Amazon earnings CSV upload, save as earnings_latest.csv,
    then immediately run the network match scan.
    """
    from product_api import ArcherAPI
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided — send as multipart field "file"'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.csv'):
        return jsonify({'error': 'File must be a .csv'}), 400

    os.makedirs('data', exist_ok=True)
    save_path = 'data/earnings_latest.csv'
    f.save(save_path)
    logging.info(f'[SCAN] Earnings CSV uploaded: {f.filename} → {save_path}')

    a = ArcherAPI()
    result = a.asin_match_scan()
    result['uploaded_filename'] = f.filename
    return jsonify(result)


@app.route('/archer/asin_match_scan')
def archer_asin_match_scan():
    """
    Re-run network match scan against the last uploaded earnings CSV.
    Safe to call any time — fully rewrites matched_asins.json.
    """
    from product_api import ArcherAPI
    a = ArcherAPI()
    result = a.asin_match_scan()
    return jsonify(result)


@app.route('/archer/force_rescan')
def archer_force_rescan():
    """Force-regenerate matched_asins.json with current network data."""
    from product_api import ArcherAPI
    a = ArcherAPI()
    result = a.asin_match_scan()
    return jsonify(result)


@app.route('/levanta/diag')
def levanta_diag():
    """Diagnostic: show raw Levanta API product + brand shapes to verify field names."""
    from product_api import LevantaAPI
    lv = LevantaAPI()
    if not lv.api_key:
        return jsonify({'error': 'No LEVANTA_API_KEY set'})
    try:
        products_data = lv.get_products(limit=3)
        products = products_data.get('products', [])
        brands_data = lv.get_brands(access_only=False, limit=3)
        brands = (brands_data.get('brands') or brands_data.get('data') or
                  brands_data.get('items') or [])
        return jsonify({
            'products_sample': products[:3],
            'products_keys': list(products[0].keys()) if products else [],
            'brands_raw_keys': list(brands_data.keys()),
            'brands_sample': brands[:3],
            'brands_keys': list(brands[0].keys()) if brands else [],
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/archer/scan_status')
def archer_scan_status():
    """Return metadata from the last scan run (scan_meta.json)."""
    import csv as _csv
    meta_path = 'data/scan_meta.json'
    earnings_path = 'data/earnings_latest.csv'
    legacy_path = 'data/2025-Q12026 amazon asin earnings.csv'
    archer_csv_path = 'data/Archer Full Catalog 2026.csv'
    levanta_cache_path = 'data/network_cache_levanta.json'

    status = {'never_run': not os.path.exists(meta_path)}
    if not status['never_run']:
        with open(meta_path) as f:
            status.update(json.load(f))

    status['csv_uploaded'] = os.path.exists(earnings_path)
    status['csv_filename'] = (
        'earnings_latest.csv' if os.path.exists(earnings_path)
        else ('2025-Q12026 amazon asin earnings.csv' if os.path.exists(legacy_path) else None)
    )

    # Catalog sizes for stat bar
    archer_size = 0
    if os.path.exists(archer_csv_path):
        try:
            with open(archer_csv_path, newline='', encoding='utf-8-sig') as f:
                archer_size = sum(1 for row in _csv.DictReader(f) if (row.get('ASIN') or '').strip())
        except Exception:
            pass
    status['archer_catalog_size'] = archer_size

    levanta_size = 0
    if os.path.exists(levanta_cache_path):
        try:
            with open(levanta_cache_path) as f:
                lv = json.load(f)
            levanta_size = len(lv) if isinstance(lv, list) else len(lv.keys())
        except Exception:
            pass
    status['levanta_catalog_size'] = levanta_size

    return jsonify(status)

@app.route('/archer/search')
def archer_search():
    """Search Archer and/or Levanta catalogs. Supports network=archer|levanta|both."""
    from product_api import ArcherAPI, LevantaAPI
    q = request.args.get('q', '').strip()
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

@app.route('/archer/levanta_match_scan')
def archer_levanta_match_scan():
    """
    Read all ASINs from Steph's Amazon earnings CSV, cross-reference
    against accessible Levanta products, and save matches to data/levanta_matches.json.
    """
    import csv
    from product_api import LevantaAPI

    CSV_PATH = os.path.join(os.path.dirname(__file__), 'data', 'Amazon_Earnings_2026.csv')
    OUT_PATH = os.path.join(os.path.dirname(__file__), 'data', 'levanta_matches.json')

    if not os.path.exists(CSV_PATH):
        return jsonify({'error': 'Amazon_Earnings_2026.csv not found in data/'}), 404

    # Read CSV — row 1 is report title (skip), row 2 is headers
    steph_asins = {}  # asin -> {revenue, units, name, category}
    with open(CSV_PATH, newline='', encoding='utf-8-sig') as f:
        next(f)  # skip "Fee-Earnings reports from..." title row
        reader = csv.DictReader(f)
        for row in reader:
            asin = (row.get('ASIN') or '').strip()
            if not asin:
                continue
            try:
                revenue = float(row.get('Revenue($)') or 0)
                units = int(row.get('Items Shipped') or 0)
            except (ValueError, TypeError):
                revenue, units = 0, 0
            if asin in steph_asins:
                steph_asins[asin]['revenue'] += revenue
                steph_asins[asin]['units'] += units
            else:
                steph_asins[asin] = {
                    'name': (row.get('Name') or '').strip(),
                    'category': (row.get('Category') or '').strip(),
                    'revenue': revenue,
                    'units': units,
                }

    # Fetch all accessible Levanta ASINs
    lv = LevantaAPI()
    try:
        levanta_map = lv.get_all_accessible_asins()
    except Exception as e:
        logging.error(f"[LEVANTA] get_all_accessible_asins failed: {e}")
        return jsonify({'error': str(e)}), 500

    # Find matches
    matches = []
    for asin, steph in steph_asins.items():
        if asin in levanta_map:
            lv_data = levanta_map[asin]
            matches.append({
                'asin': asin,
                'name': steph['name'],
                'category': steph['category'],
                'steph_revenue': round(steph['revenue'], 2),
                'steph_units': steph['units'],
                'levanta_commission': lv_data['commission_pct'],
                'levanta_commission_rate': lv_data['commission'],
                'levanta_title': lv_data['title'],
                'levanta_brand': lv_data['brand'],
            })

    # Sort by Steph's revenue descending
    matches.sort(key=lambda x: x['steph_revenue'], reverse=True)

    # Save to file
    with open(OUT_PATH, 'w') as f:
        json.dump(matches, f, indent=2)

    return jsonify({
        'steph_asins': len(steph_asins),
        'levanta_asins': len(levanta_map),
        'matches_found': len(matches),
        'top_matches': matches[:10],
        'saved_to': 'data/levanta_matches.json',
    })


@app.route('/archer/backfill_images')
def archer_backfill_images():
    """One-time route to populate image URLs for matched ASINs."""
    from product_api import ArcherAPI
    a = ArcherAPI()
    matched = a._load_matched_json()
    asins = [p['asin'] for p in matched]
    updated = a.backfill_images(asins)
    return jsonify({'updated': updated, 'total': len(asins)})

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
    a = ArcherAPI()
    product = a.get_by_asins([asin])

    # If found in cache but no image, force a live lookup to backfill
    if product and not product[0].get('image_encoded_string'):
        product = []

    if not product:
        try:
            r = req.get('https://api.archeraffiliates.com/get_single_product',
                headers={"Authorization": f"Bearer {a._get_token()}"},
                params={"asin": asin}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                img = data.get("image_encoded_string", "")
                if img:
                    conn = a._db_connect()
                    conn.execute("UPDATE products SET image_encoded_string=? WHERE asin=?", (img, asin))
                    conn.commit()
                    conn.close()
                return jsonify({"product": {
                    "asin": data.get("ASIN"),
                    "product_name": data.get("product_name"),
                    "company_name": data.get("company_name"),
                    "price": data.get("price"),
                    "commission_payout": data.get("commission_payout_aff"),
                    "image_encoded_string": img,
                    "product_category": data.get("product_category")
                }})
        except Exception as e:
            logging.error(f"[ARCHER] Product lookup failed for {asin}: {e}")
        return jsonify({"error": "Product not found"}), 404
    return jsonify({"product": product[0]})

@app.route('/archer/generate_caption', methods=['POST'])
def archer_generate_caption():
    data = request.get_json() or {}
    products_str = data.get('products', '')
    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=200,
            system="""You are Steph from @EverydaywithSteph and the Mommy & Me Collective.
Write a short, enthusiastic Facebook/Instagram caption for a product collage.
Keep it 2-3 sentences max. Warm, mom-to-mom tone. Light emojis.
Mention the products naturally. End with a call to action like "Links in bio!" or "Shop below! 👇"
Return ONLY the caption text, nothing else.""",
            messages=[{"role": "user", "content": f"Write a caption for these products: {products_str}"}]
        )
        return jsonify({"caption": message.content[0].text.strip()})
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
            system="""You are writing ad copy for Steph (@EverydaywithSteph / Mommy & Me Collective).
Steph's voice: warm, enthusiastic, mom-to-mom, like texting your best friend about a deal.
Light emoji use. Direct and honest. Always mentions the deal or price.

Return ONLY valid JSON — no preamble, no markdown, no backticks.
Format: {"variants": [{"headline": "...", "primary_text": "...", "cta": "..."}, ...]}
Generate exactly 3 variants. Each should have a different angle:
- Variant A: deal/price focused
- Variant B: product benefit focused
- Variant C: social proof / mom recommendation angle
Keep headlines under 40 chars. Primary text 2-3 sentences max.""",
            messages=[{
                "role": "user",
                "content": f"Write 3 ad copy variants for a {campaign_type} linking to {routing}. Products: {products}"
            }]
        )

        raw = message.content[0].text.strip().replace('```json', '').replace('```', '').strip()
        parsed = json.loads(raw)
        variants = parsed.get('variants', [])

        # Generate attribution links using actual selected product ASINs
        a = ArcherAPI()
        asin = product_asins[0] if product_asins else None
        for i, v in enumerate(variants):
            label = f"steph-{slug}-var{['a','b','c'][i]}"
            if asin:
                link = a.generate_link(asin, label=label)
                if link:
                    v['attribution_url'] = link.get('attribution_link') or link.get('url') or ''
                    v['label'] = label

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
VALID_PLACEMENTS = {
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
    link_label = f"URLgenius · {utm_source}/{utm_medium} · {utm_content}"
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


@app.route('/urlgenius/test')
def urlgenius_test():
    from product_api import URLGeniusAPI
    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'error': 'URLGENIUS_API_KEY not set. Add it to .env or Replit Secrets.'}), 400
    try:
        result = ug.create_link(
            destination_url="https://www.amazon.com/dp/B0C84VRPWL",
            utm_source="steph-ai", utm_medium="ai-agent",
            utm_campaign="mommymeai-test", utm_content="B0C84VRPWL"
        )
        return jsonify({'status': 'connected', 'result': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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


@app.route('/urlgenius/sync_registry')
def urlgenius_sync_registry():
    """
    Page through all URLGenius links via the API and rebuild the local registry.
    Handles 20K+ links. Can take 30–60s on a cold pull.
    """
    from product_api import URLGeniusAPI
    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'error': 'URLGENIUS_API_KEY not set'}), 400
    try:
        n = ug.seed_registry()
        return jsonify({'status': 'ok', 'links_synced': n, 'registry_size': len(ug._registry)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/urlgenius/links')
def urlgenius_list_links():
    """
    Return URLGenius links.
    No params → all links in one shot.
    ?page=N   → paginated mode (~50/page) with meta.pagination.
    """
    from product_api import URLGeniusAPI
    ug = URLGeniusAPI()
    if not ug.api_key:
        return jsonify({'error': 'URLGENIUS_API_KEY not set'}), 400
    try:
        page = request.args.get('page')
        return jsonify(ug.list_links(page=int(page) if page else None))
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


@app.route('/levanta/refresh_cache')
def levanta_refresh_cache():
    """Rebuild network_cache_levanta.json from live API (brand names + images included)."""
    from product_api import LevantaNetworkMatcher
    try:
        matcher = LevantaNetworkMatcher()
        asin_map = matcher.get_asin_data()
        if not asin_map:
            return jsonify({'error': 'No data returned — check LEVANTA_API_KEY'}), 500
        brands = len({v.get('brand') for v in asin_map.values() if v.get('brand')})
        return jsonify({'status': 'ok', 'asins': len(asin_map), 'brands': brands})
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
