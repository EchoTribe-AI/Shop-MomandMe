"""
Archived routes — removed from active app.py.
These were operational/diagnostic routes no longer needed in the main flow.
Templates for /plan, /architecture, /connections are in archive/templates/.

To restore: copy a route back into app.py and register on the Flask app instance.
"""

import os
import json
import logging
import csv as _csv
from flask import request, jsonify, send_from_directory


# ── /plan, /architecture, /connections ───────────────────────────────────────
# Served static HTML files from repo root.
# Templates: archive/templates/steph-ai-plan.html
#            archive/templates/steph-architecture.html
#            archive/templates/steph-connection-map.html

# @app.route('/plan')
# def plan():
#     return send_from_directory('.', 'steph-ai-plan.html')

# @app.route('/architecture')
# def architecture():
#     return send_from_directory('.', 'steph-architecture.html')

# @app.route('/connections')
# def connections():
#     return send_from_directory('.', 'steph-connection-map.html')


# ── /archer/matched ───────────────────────────────────────────────────────────
# @app.route('/archer/matched')
# def archer_matched():
#     """Return matched ASINs from matched_asins.json with pagination."""
#     from product_api import ArcherAPI
#     a = ArcherAPI()
#     limit  = min(int(request.args.get('limit', 20)), 100)
#     offset = int(request.args.get('offset', 0))
#     all_products = a._load_matched_json()
#     total = len(all_products)
#     page  = all_products[offset:offset + limit]
#     return jsonify({'products': page, 'total': total, 'has_more': offset + limit < total})


# ── /archer/upload_earnings ───────────────────────────────────────────────────
# @app.route('/archer/upload_earnings', methods=['POST'])
# def archer_upload_earnings():
#     """
#     Accept an Amazon earnings CSV upload, save as earnings_latest.csv,
#     then immediately run the network match scan.
#     """
#     from product_api import ArcherAPI
#     if 'file' not in request.files:
#         return jsonify({'error': 'No file provided — send as multipart field "file"'}), 400
#     f = request.files['file']
#     if not f.filename.lower().endswith('.csv'):
#         return jsonify({'error': 'File must be a .csv'}), 400
#     os.makedirs('data', exist_ok=True)
#     save_path = 'data/earnings_latest.csv'
#     f.save(save_path)
#     logging.info(f'[SCAN] Earnings CSV uploaded: {f.filename} → {save_path}')
#     a = ArcherAPI()
#     result = a.asin_match_scan()
#     result['uploaded_filename'] = f.filename
#     return jsonify(result)


# ── /archer/asin_match_scan ───────────────────────────────────────────────────
# @app.route('/archer/asin_match_scan')
# def archer_asin_match_scan():
#     """
#     Re-run network match scan against the last uploaded earnings CSV.
#     Safe to call any time — fully rewrites matched_asins.json.
#     """
#     from product_api import ArcherAPI
#     a = ArcherAPI()
#     result = a.asin_match_scan()
#     return jsonify(result)


# ── /archer/force_rescan ──────────────────────────────────────────────────────
# @app.route('/archer/force_rescan')
# def archer_force_rescan():
#     """Force-regenerate matched_asins.json with current network data."""
#     from product_api import ArcherAPI
#     a = ArcherAPI()
#     result = a.asin_match_scan()
#     return jsonify(result)


# ── /levanta/diag ─────────────────────────────────────────────────────────────
# @app.route('/levanta/diag')
# def levanta_diag():
#     """Diagnostic: show raw Levanta API product + brand shapes to verify field names."""
#     from product_api import LevantaAPI
#     lv = LevantaAPI()
#     if not lv.api_key:
#         return jsonify({'error': 'No LEVANTA_API_KEY set'})
#     try:
#         products_data = lv.get_products(limit=3)
#         products = products_data.get('products', [])
#         brands_data = lv.get_brands(access_only=False, limit=3)
#         brands = (brands_data.get('brands') or brands_data.get('data') or
#                   brands_data.get('items') or [])
#         return jsonify({
#             'products_sample': products[:3],
#             'products_keys': list(products[0].keys()) if products else [],
#             'brands_raw_keys': list(brands_data.keys()),
#             'brands_sample': brands[:3],
#             'brands_keys': list(brands[0].keys()) if brands else [],
#         })
#     except Exception as e:
#         return jsonify({'error': str(e)})


# ── /archer/scan_status ───────────────────────────────────────────────────────
# @app.route('/archer/scan_status')
# def archer_scan_status():
#     """Return metadata from the last scan run (scan_meta.json)."""
#     meta_path = 'data/scan_meta.json'
#     earnings_path = 'data/earnings_latest.csv'
#     legacy_path = 'data/2025-Q12026 amazon asin earnings.csv'
#     archer_csv_path = 'data/Archer Full Catalog 2026.csv'
#     levanta_cache_path = 'data/network_cache_levanta.json'
#
#     status = {'never_run': not os.path.exists(meta_path)}
#     if not status['never_run']:
#         with open(meta_path) as f:
#             status.update(json.load(f))
#
#     status['csv_uploaded'] = os.path.exists(earnings_path)
#     status['csv_filename'] = (
#         'earnings_latest.csv' if os.path.exists(earnings_path)
#         else ('2025-Q12026 amazon asin earnings.csv' if os.path.exists(legacy_path) else None)
#     )
#
#     archer_size = 0
#     if os.path.exists(archer_csv_path):
#         try:
#             with open(archer_csv_path, newline='', encoding='utf-8-sig') as f:
#                 archer_size = sum(1 for row in _csv.DictReader(f) if (row.get('ASIN') or '').strip())
#         except Exception:
#             pass
#     status['archer_catalog_size'] = archer_size
#
#     levanta_size = 0
#     if os.path.exists(levanta_cache_path):
#         try:
#             with open(levanta_cache_path) as f:
#                 lv = json.load(f)
#             levanta_size = len(lv) if isinstance(lv, list) else len(lv.keys())
#         except Exception:
#             pass
#     status['levanta_catalog_size'] = levanta_size
#
#     return jsonify(status)


# ── /archer/levanta_match_scan ────────────────────────────────────────────────
# @app.route('/archer/levanta_match_scan')
# def archer_levanta_match_scan():
#     """
#     Read all ASINs from Steph's Amazon earnings CSV, cross-reference
#     against accessible Levanta products, and save matches to data/levanta_matches.json.
#     """
#     import csv
#     from product_api import LevantaAPI
#
#     CSV_PATH = os.path.join(os.path.dirname(__file__), 'data', 'Amazon_Earnings_2026.csv')
#     OUT_PATH = os.path.join(os.path.dirname(__file__), 'data', 'levanta_matches.json')
#
#     if not os.path.exists(CSV_PATH):
#         return jsonify({'error': 'Amazon_Earnings_2026.csv not found in data/'}), 404
#
#     steph_asins = {}
#     with open(CSV_PATH, newline='', encoding='utf-8-sig') as f:
#         next(f)
#         reader = csv.DictReader(f)
#         for row in reader:
#             asin = (row.get('ASIN') or '').strip()
#             if not asin:
#                 continue
#             try:
#                 revenue = float(row.get('Revenue($)') or 0)
#                 units = int(row.get('Items Shipped') or 0)
#             except (ValueError, TypeError):
#                 revenue, units = 0, 0
#             if asin in steph_asins:
#                 steph_asins[asin]['revenue'] += revenue
#                 steph_asins[asin]['units'] += units
#             else:
#                 steph_asins[asin] = {
#                     'name': (row.get('Name') or '').strip(),
#                     'category': (row.get('Category') or '').strip(),
#                     'revenue': revenue, 'units': units,
#                 }
#
#     lv = LevantaAPI()
#     try:
#         levanta_map = lv.get_all_accessible_asins()
#     except Exception as e:
#         logging.error(f"[LEVANTA] get_all_accessible_asins failed: {e}")
#         return jsonify({'error': str(e)}), 500
#
#     matches = []
#     for asin, steph in steph_asins.items():
#         if asin in levanta_map:
#             lv_data = levanta_map[asin]
#             matches.append({
#                 'asin': asin, 'name': steph['name'], 'category': steph['category'],
#                 'steph_revenue': round(steph['revenue'], 2), 'steph_units': steph['units'],
#                 'levanta_commission': lv_data['commission_pct'],
#                 'levanta_commission_rate': lv_data['commission'],
#                 'levanta_title': lv_data['title'], 'levanta_brand': lv_data['brand'],
#             })
#
#     matches.sort(key=lambda x: x['steph_revenue'], reverse=True)
#     with open(OUT_PATH, 'w') as f:
#         json.dump(matches, f, indent=2)
#
#     return jsonify({
#         'steph_asins': len(steph_asins), 'levanta_asins': len(levanta_map),
#         'matches_found': len(matches), 'top_matches': matches[:10],
#         'saved_to': 'data/levanta_matches.json',
#     })


# ── /archer/backfill_images ───────────────────────────────────────────────────
# @app.route('/archer/backfill_images')
# def archer_backfill_images():
#     """One-time route to populate image URLs for matched ASINs."""
#     from product_api import ArcherAPI
#     a = ArcherAPI()
#     matched = a._load_matched_json()
#     asins = [p['asin'] for p in matched]
#     updated = a.backfill_images(asins)
#     return jsonify({'updated': updated, 'total': len(asins)})


# ── /urlgenius/test ───────────────────────────────────────────────────────────
# @app.route('/urlgenius/test')
# def urlgenius_test():
#     from product_api import URLGeniusAPI
#     ug = URLGeniusAPI()
#     if not ug.api_key:
#         return jsonify({'error': 'URLGENIUS_API_KEY not set. Add it to .env or Replit Secrets.'}), 400
#     try:
#         result = ug.create_link(
#             destination_url="https://www.amazon.com/dp/B0C84VRPWL",
#             utm_source="steph-ai", utm_medium="ai-agent",
#             utm_campaign="mommymeai-test", utm_content="B0C84VRPWL"
#         )
#         return jsonify({'status': 'connected', 'result': result})
#     except Exception as e:
#         return jsonify({'error': str(e)}), 500
