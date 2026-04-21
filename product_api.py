"""
EchoTribe Product API Integration
Handles Walmart, Amazon (via Crawlbase), Impact affiliate link generation, and Archer Affiliates
"""

import os
import requests
import json
import hmac
import hashlib
import logging
import sqlite3
import time
from datetime import datetime, timedelta
import time
import base64
import uuid
from typing import List, Dict, Optional
from urllib.parse import urlencode, quote


class WalmartAPI:
    """Walmart Affiliate Product API integration with RSA-SHA256 authentication"""
    
    BASE_URL = "https://developer.api.walmart.com"
    
    def __init__(self):
        self.consumer_id = os.environ.get('WALMART_API_PUBLIC_KEY')
        raw_key = os.environ.get('WALMART_API_PRIVATE_KEY') or ""
        # Fix: Replace escaped newlines (\n as two chars) with actual newlines
        self.private_key_pem = raw_key.replace("\\n", "\n")
        self.publisher_id = os.environ.get('WALMART_PUBLISHER_ID') or self.consumer_id
    
    def search(self, query: str, max_results: int = 3) -> List[Dict]:
        """Search Walmart products with RSA-SHA256 authentication"""
        endpoint = f"{self.BASE_URL}/api-proxy/service/affil/product/v2/search"
        
        params = {
            'query': query,
            'numItems': max_results,
            'format': 'json',
            'publisherId': self.publisher_id
        }
        
        try:
            headers = self._build_headers(endpoint, params)
            if not headers:
                return []
            
            response = requests.get(endpoint, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            products = []
            for item in data.get('items', []):
                products.append({
                    'name': item.get('name', ''),
                    'price': f"${item.get('salePrice', 0):.2f}",
                    'was': f"${item.get('msrp', 0):.2f}" if item.get('msrp', 0) > item.get('salePrice', 0) else '',
                    'retailer': 'Walmart',
                    'sku': str(item.get('itemId', '')),
                    'url': item.get('productUrl', ''),
                    'image': item.get('largeImage', ''),
                    'category': item.get('categoryPath', '').split('/')[0] if item.get('categoryPath') else '',
                    'emoji': self._category_to_emoji(item.get('categoryPath', ''))
                })
            
            return products
            
        except requests.exceptions.RequestException as e:
            return []
        except Exception as e:
            return []
    
    def _build_headers(self, endpoint: str, params: Dict) -> Dict:
        """Build RSA-signed headers for Walmart Affiliate API"""
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.backends import default_backend
            import sys
            
            # Timestamp in milliseconds
            ts = str(int(time.time() * 1000))
            
            # Exact string to sign format: consumerId\ntimestamp\nkeyVersion\n
            string_to_sign = f"{self.consumer_id}\n{ts}\n1\n"
            
            # Load private key
            private_key = serialization.load_pem_private_key(
                self.private_key_pem.encode("utf-8"),
                password=None,
                backend=default_backend()
            )
            
            # Sign with PKCS1v15 + SHA256
            sig_bytes = private_key.sign(
                string_to_sign.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            
            # Base64 encode the signature
            signature = base64.b64encode(sig_bytes).decode("utf-8")
            
            # Build all 6 required headers
            headers = {
                "WM_CONSUMER.ID": self.consumer_id,
                "WM_CONSUMER.INTIMESTAMP": ts,
                "WM_SEC.KEY_VERSION": "1",
                "WM_SEC.AUTH_SIGNATURE": signature,
                "WM_CONSUMER.CHANNEL.TYPE": "AFFILIATE",
                "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
                "Accept": "application/json"
            }
            
            return headers
        except Exception as e:
            return {}
    
    def _category_to_emoji(self, category_path: str) -> str:
        """Map Walmart category to emoji"""
        category_lower = category_path.lower()
        
        if 'toy' in category_lower:
            return '🧸'
        elif 'baby' in category_lower:
            return '👶'
        elif 'home' in category_lower or 'furniture' in category_lower:
            return '🏠'
        elif 'beauty' in category_lower or 'health' in category_lower:
            return '💄'
        elif 'electronic' in category_lower:
            return '📱'
        elif 'cloth' in category_lower or 'fashion' in category_lower:
            return '👕'
        elif 'food' in category_lower or 'grocery' in category_lower:
            return '🍎'
        elif 'sport' in category_lower:
            return '⚽'
        else:
            return '🏪'


class CrawlbaseAPI:
    """Crawlbase API for Amazon product scraping"""
    
    BASE_URL = "https://api.crawlbase.com/"
    
    def __init__(self):
        self.token = os.environ.get('CRAWLBASE_JS_TOKEN')
    
    def search_amazon(self, query: str, max_results: int = 3) -> List[Dict]:
        """Search Amazon products via Crawlbase"""
        search_url = f"https://www.amazon.com/s?k={quote(query)}"
        
        params = {
            'token': self.token,
            'url': search_url,
            'ajax_wait': 'true',
            'page_wait': '2000'
        }
        
        try:
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            products = self._parse_amazon_search(response.text, max_results)
            return products
            
        except requests.exceptions.RequestException as e:
            print(f"Crawlbase API error: {e}")
            return []
    
    def get_amazon_product(self, asin: str) -> Optional[Dict]:
        """Get detailed Amazon product info by ASIN"""
        product_url = f"https://www.amazon.com/dp/{asin}"
        
        params = {
            'token': self.token,
            'url': product_url,
            'ajax_wait': 'true',
            'page_wait': '2000'
        }
        
        try:
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            product = self._parse_amazon_product(response.text, asin)
            return product
            
        except requests.exceptions.RequestException as e:
            print(f"Crawlbase product fetch error: {e}")
            return None
    
    def _parse_amazon_search(self, html: str, max_results: int) -> List[Dict]:
        """Parse Amazon search results HTML"""
        products = []
        return products
    
    def _parse_amazon_product(self, html: str, asin: str) -> Optional[Dict]:
        """Parse Amazon product page HTML"""
        return None
    
    def build_affiliate_link(self, asin: str, tag: str = "mommymedeals-20") -> str:
        """Build Amazon affiliate link from ASIN"""
        return f"https://amazon.com/dp/{asin}?tag={tag}"


class ImpactAPI:
    """Impact.com API for Walmart affiliate link generation"""
    
    BASE_URL = "https://api.impact.com/Mediapartners"
    
    def __init__(self):
        self.account_sid = os.environ.get('IMPACT_ACCOUNT_SID')
        self.auth_token = os.environ.get('IMPACT_AUTH_TOKEN')
    
    def generate_walmart_link(self, product_url: str, product_id: str = None, 
                             sub_id1: str = "chat", sub_id2: str = None) -> str:
        """Generate Impact affiliate link for Walmart product"""
        
        endpoint = f"{self.BASE_URL}/{self.account_sid}/Conversions/ConversionLink"
        campaign_id = "16662"
        
        params = {
            'DestinationUrl': product_url,
            'CampaignId': campaign_id,
            'SubId1': sub_id1,
            'SubId2': sub_id2 or product_id or ''
        }
        
        auth = (self.account_sid, self.auth_token)
        
        try:
            response = requests.get(endpoint, params=params, auth=auth, timeout=10)
            response.raise_for_status()
            data = response.json()
            tracking_url = data.get('VanityUrl') or data.get('TrackingUrl')
            
            if tracking_url:
                return tracking_url
            else:
                return self._build_manual_link(product_url, product_id, sub_id1, sub_id2)
                
        except requests.exceptions.RequestException as e:
            print(f"Impact API error: {e}")
            return self._build_manual_link(product_url, product_id, sub_id1, sub_id2)
    
    def _build_manual_link(self, product_url: str, product_id: str, 
                          sub_id1: str, sub_id2: str) -> str:
        """Build Impact tracking link manually"""
        base = "https://goto.walmart.com/c/3590891/1398372/16662"
        encoded_url = quote(product_url, safe='')
        
        params = {
            'veh': 'aff',
            'u': encoded_url,
            'subId1': sub_id1,
            'subId2': sub_id2 or product_id or ''
        }
        
        return f"{base}?{urlencode(params)}"


def detect_category(query: str) -> str:
    """Detect product category from search query"""
    query_lower = query.lower()

    category_keywords = {
        'toys': ['toy', 'doll', 'action figure', 'lego', 'playset', 'puzzle', 'game'],
        'baby': ['baby', 'infant', 'newborn', 'nursery', 'stroller'],
        'kids': ['kid', 'children', 'toddler', 'preschool'],
        'beauty': ['beauty', 'makeup', 'skincare', 'fragrance', 'cosmetic', 'serum', 'moisturizer'],
        'health': ['vitamin', 'supplement', 'protein', 'wellness', 'health', 'fitness'],
        'home': ['home', 'furniture', 'decor', 'kitchen', 'bedroom', 'appliance', 'gadget'],
        'outdoor': ['outdoor', 'garden', 'patio', 'camping', 'lawn'],
        'pets': ['pet', 'dog', 'cat', 'puppy', 'kitten'],
        'electronics': ['electronic', 'bluetooth', 'speaker', 'headphone', 'phone', 'tablet'],
        'clothing': ['clothing', 'shirt', 'pants', 'dress', 'shoes', 'jacket', 'fashion'],
        'grocery': ['food', 'snack', 'grocery', 'drink', 'coffee', 'tea'],
    }

    for category, keywords in category_keywords.items():
        if any(kw in query_lower for kw in keywords):
            return category

    return 'general'


# ── NETWORK MATCHER REGISTRY ──────────────────────────────────────────────────
# Each affiliate network implements get_asin_set() → set of ASINs it supports.
# To add a new network: subclass NetworkMatcher, add to NETWORK_MATCHERS list.

class NetworkMatcher:
    name = ''
    def get_asin_set(self) -> set:
        raise NotImplementedError
    def get_asin_data(self) -> dict:
        """Optional: return asin -> enrichment dict (e.g. commission). Default empty."""
        return {}


class ArcherNetworkMatcher(NetworkMatcher):
    """
    Reads the full Archer catalog from a local CSV file (primary source).
    Tries multiple known filenames so it works on both Mac and Replit.
    SQLite is only used for image lookups, not catalog membership.
    """
    name = 'archer'
    CATALOG_CSV_PATHS = [
        'data/Archer Full Catalog 2026.csv',
        'data/EchoTribe_x_Archer_Attribution_Product_Catalog__Archer_Full_Product_Catalog_1.csv',
    ]

    def __init__(self, db_path=None):
        self.db_path = db_path

    def _open_catalog_csv(self):
        """Return (path, file_handle) for the first found catalog CSV, or (None, None)."""
        import csv as _csv
        for path in self.CATALOG_CSV_PATHS:
            if os.path.exists(path):
                return path, open(path, newline='', encoding='utf-8-sig')
        return None, None

    def get_asin_set(self) -> set:
        import csv as _csv
        path, fh = self._open_catalog_csv()
        if fh is None:
            logging.warning('[ARCHER] No catalog CSV found — get_asin_set returns empty')
            return set()
        try:
            asins = set()
            for raw_row in _csv.DictReader(fh):
                asin = raw_row.get('ASIN', '').strip()
                if asin:
                    asins.add(asin)
            logging.info(f'[ARCHER] get_asin_set: {len(asins)} ASINs from {path}')
            return asins
        except Exception as e:
            logging.warning(f'[ARCHER] get_asin_set CSV read failed: {e}')
            return set()
        finally:
            fh.close()

    def get_asin_data(self) -> dict:
        import csv as _csv
        path, fh = self._open_catalog_csv()
        if fh is not None:
            try:
                data = {}
                for raw_row in _csv.DictReader(fh):
                    row = {k.strip(): v for k, v in raw_row.items()}
                    asin = (row.get('ASIN') or '').strip()
                    if not asin:
                        continue
                    raw_price = (row.get('Product Price') or '').strip()
                    if raw_price.lower() in ('nan', 'none', ''):
                        raw_price = ''
                    data[asin] = {
                        'product_name':    (row.get('Product Titile') or row.get('Product Title') or '').strip(),
                        'brand':           (row.get('Brand') or '').strip(),
                        'price':           raw_price,
                        'commission':      (row.get('Affiliate Commission Payout') or '').strip(),
                        'archer_category': (row.get('Category') or '').strip(),
                        'reviews':         (row.get('Total Reviews') or '').strip(),
                        'rating':          (row.get('Average Rating') or '').strip(),
                    }
                logging.info(f'[ARCHER] get_asin_data: {len(data)} ASINs from {path}')
                return data
            except Exception as e:
                logging.warning(f'[ARCHER] CSV read failed: {e}')
            finally:
                fh.close()

        logging.warning('[ARCHER] No catalog CSV found — catalog will be empty')
        return {}


class LevantaNetworkMatcher(NetworkMatcher):
    """
    Fetches accessible ASINs live from LevantaAPI, writes them to
    data/network_cache_levanta.json, falls back to cache on API failure.
    """
    name = 'levanta'
    CACHE_PATH = 'data/network_cache_levanta.json'

    def get_asin_set(self) -> set:
        return set(self.get_asin_data().keys())

    def get_asin_data(self) -> dict:
        # Try live API first
        try:
            lv = LevantaAPI()
            if lv.api_key:
                asin_map = lv.get_all_accessible_asins()
                os.makedirs('data', exist_ok=True)
                with open(self.CACHE_PATH, 'w') as f:
                    json.dump(asin_map, f)  # save full dict with brand/commission/title
                logging.info(f"[LEVANTA] get_asin_data: {len(asin_map)} ASINs from live API, cache written")
                return asin_map
        except Exception as e:
            logging.warning(f"[LEVANTA] Live API failed, falling back to cache: {e}")

        # Fall back to cache — supports both legacy list and new dict format
        if not os.path.exists(self.CACHE_PATH):
            return {}
        try:
            with open(self.CACHE_PATH) as f:
                data = json.load(f)
            if isinstance(data, list):
                logging.info(f"[LEVANTA] get_asin_data: {len(data)} ASINs from cache (legacy list, no metadata)")
                return {a: {} for a in data}
            elif isinstance(data, dict):
                logging.info(f"[LEVANTA] get_asin_data: {len(data)} ASINs from cache (full metadata)")
                return data
            return {}
        except Exception as e:
            logging.warning(f"[LEVANTA] Cache read failed: {e}")
            return {}


class ArcherAPI:
    """Archer Affiliates API client with auto-refreshing bearer token and local SQLite catalog cache."""

    ARCHER_BASE = "https://api.archeraffiliates.com"
    CACHE_DB = "data/archer_catalog.db"
    CACHE_TTL_HOURS = 24
    MATCHED_ASINS_PATH = "data/matched_asins.json"

    def __init__(self):
        self.token = None
        self.token_expires = None
        self._init_cache()
        self._seed_from_json()
        self._maybe_rescan()

    def _maybe_rescan(self):
        """Trigger a fresh scan if matched_asins.json is missing or stale."""
        try:
            if not os.path.exists(self.MATCHED_ASINS_PATH):
                logging.info("[SCAN] matched_asins.json missing — triggering initial scan")
                self.asin_match_scan()
                return
            matched = self._load_matched_json()
            if matched and 'networks' not in matched[0]:
                logging.info("[SCAN] matched_asins.json is stale — triggering rescan")
                self.asin_match_scan()
        except Exception as e:
            logging.warning(f"[SCAN] Startup rescan check failed: {e}")

    # ── AUTH ──────────────────────────────────────────────

    def _get_token(self):
        if self.token and datetime.now() < self.token_expires:
            return self.token
        r = requests.post(f"{self.ARCHER_BASE}/token", data={
            "username": os.environ.get("ARCHER_USERNAME"),
            "password": os.environ.get("ARCHER_PASSWORD")
        })
        r.raise_for_status()
        self.token = r.json()["access_token"]
        self.token_expires = datetime.now() + timedelta(minutes=55)
        logging.info("[ARCHER] Token refreshed")
        return self.token

    def _headers(self):
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ── CATALOG CACHE ─────────────────────────────────────

    def _db_connect(self, timeout=30):
        """Open a DB connection with WAL mode and a lock timeout."""
        conn = sqlite3.connect(self.CACHE_DB, timeout=timeout)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # DB may be locked briefly on startup; WAL upgrade will succeed later
        return conn

    def _init_cache(self):
        os.makedirs("data", exist_ok=True)
        conn = self._db_connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                asin TEXT PRIMARY KEY,
                brand_id TEXT,
                company_name TEXT,
                product_name TEXT,
                price TEXT,
                commission_payout TEXT,
                product_category TEXT,
                sub_category TEXT,
                avg_rating TEXT,
                total_reviews TEXT,
                image_encoded_string TEXT,
                deal_json TEXT,
                product_status TEXT,
                steph_revenue REAL,
                steph_units INTEGER,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collages (
                slug TEXT PRIMARY KEY,
                products_json TEXT,
                layout TEXT DEFAULT 'layout-2',
                theme TEXT DEFAULT 'coral',
                caption TEXT,
                direct_to_amazon INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                click_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS click_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT,
                slug TEXT,
                fbclid TEXT,
                attribution_url TEXT,
                clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                slug TEXT PRIMARY KEY,
                campaign_type TEXT DEFAULT 'organic',
                routing TEXT DEFAULT 'landing',
                products_json TEXT,
                variants_json TEXT,
                spend_budget REAL DEFAULT 0,
                forecast_roas TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def _seed_from_json(self):
        """Seed SQLite from matched_asins.json if DB is empty."""
        if not os.path.exists(self.MATCHED_ASINS_PATH):
            return
        conn = self._db_connect()
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count > 0:
            conn.close()
            return
        try:
            with open(self.MATCHED_ASINS_PATH) as f:
                products = json.load(f)
            for p in products:
                conn.execute("""
                    INSERT OR IGNORE INTO products
                    (asin, company_name, product_name, price, commission_payout,
                     product_category, avg_rating, total_reviews, product_status,
                     steph_revenue, steph_units, cached_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                """, (
                    p.get("asin"), p.get("brand"), p.get("product_name"),
                    p.get("price"), p.get("commission"),
                    p.get("archer_category"), p.get("rating"), p.get("reviews"),
                    "active", p.get("steph_revenue", 0), p.get("steph_units", 0)
                ))
            conn.commit()
            logging.info(f"[ARCHER] Seeded {len(products)} products from matched_asins.json")
        except Exception as e:
            logging.error(f"[ARCHER] Seed from JSON failed: {e}")
        finally:
            conn.close()

    def get_matched_products_enriched(self):
        """
        Load matched ASINs and enrich with live data from both Archer SQLite
        and Levanta API. If a product exists in both, return both commission rates.
        """
        matched = self._load_matched_json()
        conn = self._db_connect()
        conn.row_factory = sqlite3.Row

        results = []
        lv = LevantaAPI()

        for p in matched:
            asin = p.get('asin')
            # Get Archer data from SQLite
            archer_row = conn.execute(
                "SELECT * FROM products WHERE asin=?", (asin,)
            ).fetchone()

            archer_data = dict(archer_row) if archer_row else {
                'asin': asin,
                'product_name': p.get('product_name'),
                'company_name': p.get('brand'),
                'commission_payout': p.get('commission'),
                'product_category': p.get('archer_category'),
                'price': p.get('price'),
                'avg_rating': p.get('rating'),
                'total_reviews': p.get('reviews'),
                'steph_revenue': p.get('steph_revenue', 0),
                'steph_units': p.get('steph_units', 0),
            }
            archer_data['source'] = 'archer'
            archer_data['networks'] = ['archer']

            # Check if Levanta has this ASIN too
            try:
                lv_product = lv.get_product_by_asin(asin)
                if lv_product:
                    lv_comm = lv_product.get('commission', 0)
                    archer_data['levanta_commission'] = f"{int(lv_comm * 100)}%"
                    archer_data['networks'] = ['archer', 'levanta']
                    # Use Levanta image if Archer has none
                    if not archer_data.get('image_encoded_string'):
                        archer_data['image_encoded_string'] = lv_product.get('image') or ''
            except Exception:
                pass

            results.append(archer_data)

        conn.close()
        # Sort by steph_revenue descending
        results.sort(key=lambda x: x.get('steph_revenue', 0) or 0, reverse=True)
        return results

    def _cache_is_fresh(self):
        conn = self._db_connect()
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key='last_full_sync'"
        ).fetchone()
        conn.close()
        if not row:
            return False
        last_sync = datetime.fromisoformat(row[0])
        return datetime.now() - last_sync < timedelta(hours=self.CACHE_TTL_HOURS)

    def sync_catalog(self, force=False):
        """Pull full Archer catalog into SQLite. Skips if cache is fresh unless forced."""
        if not force and self._cache_is_fresh():
            logging.info("[ARCHER] Catalog cache is fresh, skipping sync")
            return

        logging.info("[ARCHER] Starting full catalog sync...")
        page, limit, total_synced = 1, 100, 0
        conn = self._db_connect()

        while True:
            try:
                r = requests.get(f"{self.ARCHER_BASE}/getproducts",
                    headers=self._headers(),
                    params={"page": page, "limit": limit},
                    timeout=30)
                r.raise_for_status()
                data = r.json()
                products = data.get("product_catalog", [])

                if not products:
                    break

                for p in products:
                    conn.execute("""
                        INSERT OR REPLACE INTO products
                        (asin, brand_id, company_name, product_name, price,
                         commission_payout, product_category, sub_category,
                         avg_rating, total_reviews, image_encoded_string,
                         deal_json, product_status, cached_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    """, (
                        p.get("ASIN"), p.get("brand_id"), p.get("company_name"),
                        p.get("product_name"), p.get("price"),
                        p.get("commission_payout_aff"), p.get("product_category"),
                        json.dumps(p.get("sub_category", [])),
                        p.get("avg_rating"), p.get("total_reviews"),
                        p.get("image_encoded_string"),
                        json.dumps(p.get("deal", {})),
                        p.get("product_status", "active")
                    ))

                total_synced += len(products)
                logging.info(f"[ARCHER] Synced page {page}, total: {total_synced}")

                if len(products) < limit:
                    break
                page += 1
                time.sleep(0.3)

            except Exception as e:
                logging.error(f"[ARCHER] Catalog sync error on page {page}: {e}")
                break

        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('last_full_sync', ?)",
            (datetime.now().isoformat(),)
        )
        conn.commit()
        conn.close()
        logging.info(f"[ARCHER] Catalog sync complete. Total products: {total_synced}")

    # ── SEARCH ────────────────────────────────────────────

    def search_catalog(self, query, category=None, limit=5):
        """Search local SQLite cache, prioritizing Steph's highest-revenue products."""
        conn = self._db_connect()
        conn.row_factory = sqlite3.Row

        sql = """
            SELECT * FROM products
            WHERE product_status = 'active'
            AND (
                product_name LIKE ?
                OR company_name LIKE ?
                OR product_category LIKE ?
            )
        """
        params = [f"%{query}%", f"%{query}%", f"%{query}%"]

        if category:
            sql += " AND product_category LIKE ?"
            params.append(f"%{category}%")

        sql += " ORDER BY steph_revenue DESC, CAST(REPLACE(commission_payout, '%', '') AS REAL) DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def backfill_images(self, asins):
        """Fetch live product data for a list of ASINs and update image URLs in cache."""
        conn = self._db_connect()
        updated = 0
        for asin in asins:
            try:
                r = requests.get(f"{self.ARCHER_BASE}/get_single_product",
                    headers=self._headers(),
                    params={"asin": asin},
                    timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    img = data.get("image_encoded_string", "")
                    if img:
                        conn.execute(
                            "UPDATE products SET image_encoded_string=? WHERE asin=?",
                            (img, asin)
                        )
                        updated += 1
                time.sleep(0.2)
            except Exception as e:
                logging.warning(f"[ARCHER] Image backfill failed for {asin}: {e}")
        conn.commit()
        conn.close()
        logging.info(f"[ARCHER] Image backfill complete: {updated}/{len(asins)} updated")
        return updated

    def _load_matched_json(self):
        """Load matched_asins.json as a list of dicts."""
        try:
            with open(self.MATCHED_ASINS_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            return []

    # ── EARNINGS CSV MATCHING ─────────────────────────────

    # Canonical upload path — always written by the upload endpoint
    EARNINGS_CSV_PATH   = 'data/earnings_latest.csv'
    EARNINGS_CSV_LEGACY = 'data/2025-Q12026 amazon asin earnings.csv'
    SCAN_META_PATH      = 'data/scan_meta.json'
    LEVANTA_CACHE_PATH  = 'data/network_cache_levanta.json'

    def load_earnings_csv(self):
        """
        Parse the earnings CSV into a dict keyed by ASIN.
        Prefers data/earnings_latest.csv; falls back to legacy filename.
        Aggregates duplicate ASINs across time periods by summing numeric fields.
        """
        import csv
        path = self.EARNINGS_CSV_PATH
        if not os.path.exists(path):
            path = self.EARNINGS_CSV_LEGACY
        if not os.path.exists(path):
            logging.warning('[SCAN] No earnings CSV found. Upload via /archer/upload_earnings')
            return {}

        def clean_num(val):
            s = (val or '').replace('$', '').replace(',', '').replace('%', '').strip()
            return float(s) if s and s not in ('-', 'N/A', '') else 0.0

        earnings = {}
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Support both Amazon report formats:
                # Format A: "Product ASIN", "Total Earnings", "Items Returned"
                # Format B: "ASIN", "Revenue($)", "Returns"
                asin = (row.get('Product ASIN') or row.get('ASIN') or '').strip()
                if not asin:
                    continue
                row_data = {
                    'clicks':                 int(clean_num(row.get('Clicks', '0'))),
                    'items_ordered':          int(clean_num(row.get('Items Ordered', '0'))),
                    'direct_ordered':         int(clean_num(row.get('Direct Items Ordered', '0'))),
                    'conversion_rate':        row.get('Product Conversion Rate', '').strip(),
                    'amazon_commission_rate': row.get('Commission Rate', '').strip(),
                    'items_shipped':          int(clean_num(row.get('Items Shipped', '0'))),
                    'items_returned':         int(clean_num(row.get('Items Returned') or row.get('Returns', '0'))),
                    'shipped_revenue':        clean_num(row.get('Items Shipped Revenue', '0')),
                    'total_earnings':         clean_num(row.get('Total Earnings') or row.get('Revenue($)', '0')),
                    'time_period':            row.get('Time Period', '').strip(),
                    'brand':                  (row.get('Brand') or '').strip(),
                    'product_name':           (row.get('Title') or row.get('Product Title') or '').strip(),
                }
                if asin in earnings:
                    # Aggregate same ASIN across multiple time periods
                    for k in ('clicks', 'items_ordered', 'direct_ordered',
                              'items_shipped', 'items_returned'):
                        earnings[asin][k] += row_data[k]
                    earnings[asin]['shipped_revenue'] += row_data['shipped_revenue']
                    earnings[asin]['total_earnings']  += row_data['total_earnings']
                    # Keep first non-empty brand/name seen
                    if not earnings[asin]['brand'] and row_data['brand']:
                        earnings[asin]['brand'] = row_data['brand']
                    if not earnings[asin]['product_name'] and row_data['product_name']:
                        earnings[asin]['product_name'] = row_data['product_name']
                else:
                    earnings[asin] = row_data

        logging.info(f'[SCAN] Earnings CSV loaded from {path}: {len(earnings)} unique ASINs')
        return earnings

    def asin_match_scan(self):
        """
        Cross-reference earnings CSV ASINs against every registered network.
        To add a new network: subclass NetworkMatcher, add one line to matchers list below.
        Writes data/matched_asins.json and data/scan_meta.json.
        Safe to re-run at any time — fully overwrites previous results.
        """
        earnings = self.load_earnings_csv()
        if not earnings:
            return {'error': 'No earnings CSV found. Upload via /archer/upload_earnings.'}

        asin_list = list(earnings.keys())

        # ── Register networks here — add new ones as more are onboarded ──────
        matchers = [
            ArcherNetworkMatcher(db_path=self.CACHE_DB),
            LevantaNetworkMatcher(),
            # ImpactNetworkMatcher(), CJNetworkMatcher(), etc.
        ]

        # Build ASIN sets and enrichment data per network
        network_sets = {}
        network_data = {}
        for m in matchers:
            data = m.get_asin_data()
            network_data[m.name] = data
            network_sets[m.name] = set(data.keys())
            logging.info(f'[SCAN] {m.name}: {len(network_sets[m.name])} ASINs in catalog')

        # Fetch images for matched Archer ASINs from SQLite (CSV has no images)
        archer_asins = [a for a in asin_list if a in network_sets.get('archer', set())]
        archer_image_map = {}
        if archer_asins:
            try:
                conn = self._db_connect()
                conn.row_factory = sqlite3.Row
                ph = ','.join('?' * len(archer_asins))
                rows = conn.execute(
                    f'SELECT asin, image_encoded_string FROM products WHERE asin IN ({ph})',
                    archer_asins
                ).fetchall()
                conn.close()
                archer_image_map = {r['asin']: r['image_encoded_string'] or '' for r in rows}
            except Exception as e:
                logging.warning(f'[SCAN] SQLite image fetch failed: {e}')

        results = []
        for asin in asin_list:
            e = earnings[asin]
            matched_networks = [n for n, s in network_sets.items() if asin in s]

            entry = {
                'asin':                   asin,
                'clicks':                 e['clicks'],
                'items_ordered':          e['items_ordered'],
                'direct_ordered':         e['direct_ordered'],
                'conversion_rate':        e['conversion_rate'],
                'amazon_commission_rate': e['amazon_commission_rate'],
                'items_shipped':          e['items_shipped'],
                'items_returned':         e['items_returned'],
                'shipped_revenue':        e['shipped_revenue'],
                'total_earnings':         e['total_earnings'],
                'time_period':            e['time_period'],
                # Frontend-compat aliases
                'steph_revenue':          e['total_earnings'],
                'steph_units':            e['items_shipped'],
                # Network match flags — one per registered matcher
                'networks':               matched_networks,
                **{f'{n}_matched': (asin in s) for n, s in network_sets.items()},
            }

            # Enrich from Archer catalog CSV data if directly matched
            archer_csv = network_data.get('archer', {}).get(asin)
            if archer_csv:
                entry.update({
                    'product_name':         archer_csv.get('product_name', ''),
                    'brand':                archer_csv.get('brand', ''),
                    'price':                archer_csv.get('price', ''),
                    'commission':           archer_csv.get('commission', ''),
                    'archer_category':      archer_csv.get('archer_category', ''),
                    'rating':               archer_csv.get('rating', ''),
                    'reviews':              archer_csv.get('reviews', ''),
                    'image_encoded_string': archer_image_map.get(asin, ''),
                })

            lv_data = network_data.get('levanta', {}).get(asin, {})
            if lv_data:
                entry['levanta_commission'] = lv_data.get('commission_pct', '')
                entry['levanta_image'] = lv_data.get('imageUrl', '')
                if not entry.get('product_name'):
                    entry['product_name'] = lv_data.get('title', '')
                if not entry.get('brand'):
                    entry['brand'] = lv_data.get('brand', '')

            results.append(entry)

        # ── Brand-level expansion from direct matches ─────────────────────────
        # Brand comes from the network catalog (not the earnings CSV).
        # Step 1: collect brands discovered via direct ASIN matches.
        # Step 2: add NEW result entries for every catalog product from those
        #         brands that isn't already an earnings ASIN.

        earnings_asin_set = set(asin_list)  # only earnings ASINs are "original" entries

        # Archer brand expansion
        direct_archer_brands = set()
        for entry in results:
            if entry.get('archer_matched') and entry.get('brand'):
                direct_archer_brands.add(entry['brand'].lower().strip())

        archer_brand_index = {}
        for asin_val, meta in network_data.get('archer', {}).items():
            b = (meta.get('brand') or '').lower().strip()
            if b:
                archer_brand_index.setdefault(b, []).append(asin_val)

        archer_expanded = []
        for brand in direct_archer_brands:
            for archer_asin in archer_brand_index.get(brand, []):
                if archer_asin in earnings_asin_set:
                    continue  # already covered as a direct earnings match
                meta = network_data['archer'][archer_asin]
                archer_expanded.append({
                    'asin':                   archer_asin,
                    'product_name':           meta.get('product_name', ''),
                    'brand':                  meta.get('brand', ''),
                    'price':                  meta.get('price', ''),
                    'commission':             meta.get('commission', ''),
                    'archer_category':        meta.get('archer_category', ''),
                    'rating':                 meta.get('rating', ''),
                    'reviews':                meta.get('reviews', ''),
                    'image_encoded_string':   '',
                    'networks':               ['archer'],
                    'archer_matched':         True,
                    'archer_brand_match':     True,
                    'levanta_matched':        False,
                    # No earnings data for brand-expanded records
                    'clicks': 0, 'items_ordered': 0, 'direct_ordered': 0,
                    'conversion_rate': '', 'amazon_commission_rate': '',
                    'items_shipped': 0, 'items_returned': 0,
                    'shipped_revenue': 0.0, 'total_earnings': 0.0,
                    'time_period': '', 'steph_revenue': 0.0, 'steph_units': 0,
                })

        results.extend(archer_expanded)
        logging.info(
            f'[SCAN] Archer brand expansion: {len(direct_archer_brands)} brands → '
            f'{len(archer_expanded)} additional products'
        )

        # Levanta brand expansion
        # Use the live network_data already fetched (avoids re-reading cache).
        # Fall back to reading cache file only if network_data['levanta'] is empty.
        lv_data_map = network_data.get('levanta', {})
        if not lv_data_map:
            try:
                with open(self.LEVANTA_CACHE_PATH) as f:
                    raw = json.load(f)
                lv_data_map = raw if isinstance(raw, dict) else {a: {} for a in raw}
                logging.info(f'[SCAN] Levanta brand expansion: using cache file ({len(lv_data_map)} ASINs)')
            except Exception:
                lv_data_map = {}

        # Collect brands from ANY direct earnings ASIN match (Archer or Levanta).
        # A direct match = earnings ASIN was in a network catalog (not brand-expanded).
        direct_levanta_brands = set()
        for entry in results:
            if entry['asin'] not in earnings_asin_set or not entry.get('brand'):
                continue
            is_direct = (
                (entry.get('archer_matched')  and not entry.get('archer_brand_match')) or
                (entry.get('levanta_matched') and not entry.get('levanta_brand_match'))
            )
            if is_direct:
                direct_levanta_brands.add(entry['brand'].lower().strip())

        logging.info(f'[SCAN] Levanta brand expansion: checking {len(direct_levanta_brands)} brands '
                     f'({", ".join(sorted(direct_levanta_brands)) or "none"}) '
                     f'against {len(lv_data_map)} Levanta ASINs')

        # Build brand index from Levanta data (only entries with brand metadata)
        levanta_brand_index = {}
        for asin_val, meta in lv_data_map.items():
            b = (meta.get('brand') or '').lower().strip()
            if b:
                levanta_brand_index.setdefault(b, []).append(asin_val)

        logging.info(f'[SCAN] Levanta brand index: {len(levanta_brand_index)} unique brands in catalog')
        # Log which target brands were found vs missed
        for b in sorted(direct_levanta_brands):
            found = len(levanta_brand_index.get(b, []))
            logging.info(f'[SCAN]   brand "{b}": {found} Levanta products')

        levanta_expanded = []
        expanded_asin_set = earnings_asin_set | {e['asin'] for e in archer_expanded}
        for brand in direct_levanta_brands:
            for lv_asin in levanta_brand_index.get(brand, []):
                if lv_asin in expanded_asin_set:
                    continue
                meta = lv_data_map.get(lv_asin, {})
                levanta_expanded.append({
                    'asin':                   lv_asin,
                    'product_name':           meta.get('title', ''),
                    'brand':                  meta.get('brand', ''),
                    'price':                  meta.get('price', ''),
                    'commission':             '',
                    'levanta_commission':     meta.get('commission_pct', ''),
                    'levanta_image':          meta.get('imageUrl', ''),
                    'networks':               ['levanta'],
                    'archer_matched':         False,
                    'levanta_matched':        True,
                    'levanta_brand_match':    True,
                    'clicks': 0, 'items_ordered': 0, 'direct_ordered': 0,
                    'conversion_rate': '', 'amazon_commission_rate': '',
                    'items_shipped': 0, 'items_returned': 0,
                    'shipped_revenue': 0.0, 'total_earnings': 0.0,
                    'time_period': '', 'steph_revenue': 0.0, 'steph_units': 0,
                })

        results.extend(levanta_expanded)
        logging.info(
            f'[SCAN] Levanta brand expansion: {len(direct_levanta_brands)} brands → '
            f'{len(levanta_expanded)} additional products'
        )

        # Sort: most networks first, then by total earnings
        results.sort(key=lambda x: (-len(x['networks']), -x['total_earnings']))

        os.makedirs('data', exist_ok=True)
        with open(self.MATCHED_ASINS_PATH, 'w') as f:
            json.dump(results, f, indent=2)

        network_counts = {
            n: sum(1 for r in results if r.get(f'{n}_matched'))
            for n in network_sets
        }
        meta = {
            'scanned_at':  datetime.now().isoformat(),
            'total_asins': len(results),
            'networks':    network_counts,
            'any_matched': sum(1 for r in results if r['networks']),
            'unmatched':   sum(1 for r in results if not r['networks']),
        }
        with open(self.SCAN_META_PATH, 'w') as f:
            json.dump(meta, f, indent=2)

        logging.info(
            f'[SCAN] Complete: {len(results)} ASINs | '
            + ' | '.join(f'{n}={c}' for n, c in network_counts.items())
        )
        return meta

    def get_by_asin(self, asin):
        conn = self._db_connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM products WHERE asin = ?", (asin,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_by_asins(self, asin_list):
        if not asin_list:
            return []
        conn = self._db_connect()
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(asin_list))
        rows = conn.execute(
            f"SELECT * FROM products WHERE asin IN ({placeholders}) AND product_status='active'",
            asin_list
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── LINK GENERATION ──────────────────────────────────

    def generate_link(self, asin, label=None):
        """Generate a tracked Archer attribution link for a given ASIN."""
        try:
            r = requests.post(f"{self.ARCHER_BASE}/generate_attribution_link",
                headers=self._headers(),
                json={"asin": asin, "link_name": label or asin},
                timeout=10)
            r.raise_for_status()
            data = r.json()
            logging.info(f"[ARCHER] Generated link for ASIN {asin}")
            return data
        except Exception as e:
            logging.error(f"[ARCHER] Link generation failed for {asin}: {e}")
            return None

    # ── REPORTING ─────────────────────────────────────────

    def get_insights(self, start_date, end_date, asin=None, category=None, brand=None, page=1):
        """Pull product-level insights. Dates in YYYYMMDD format."""
        params = {"start_date": start_date, "end_date": end_date, "page": page, "limit": 100}
        if asin: params["productAsin"] = asin
        if category: params["productCategory"] = category
        if brand: params["brand"] = brand
        r = requests.get(f"{self.ARCHER_BASE}/insights",
            headers=self._headers(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_affiliate_id(self):
        r = requests.get(f"{self.ARCHER_BASE}/get_affiliateID", headers=self._headers())
        r.raise_for_status()
        return r.json()

    # ── PRODUCT FORMAT HELPER ─────────────────────────────

    def format_for_frontend(self, archer_product, attribution_url=None):
        """Convert Archer catalog product to the frontend product dict format."""
        deal = {}
        try:
            deal = json.loads(archer_product.get("deal_json") or "{}")
        except Exception:
            pass

        price = archer_product.get("price", "")
        sale_price = deal.get("sale_price")
        final_price = deal.get("final_price")
        display_price = f"${final_price}" if final_price else (f"${sale_price}" if sale_price else price)
        was_price = f"${deal.get('base_price')}" if deal.get("base_price") and deal.get("final_discount_%") else ""

        asin = archer_product.get("asin")
        return {
            "id": asin,
            "name": archer_product.get("product_name", ""),
            "price": display_price,
            "was": was_price,
            "retailer": "Amazon",
            "emoji": "🏹",
            "link": attribution_url or f"https://amazon.com/dp/{asin}",
            "asin": asin,
            "brand": archer_product.get("company_name", ""),
            "category": archer_product.get("product_category", ""),
            "commission": archer_product.get("commission_payout", ""),
            "rating": archer_product.get("avg_rating", ""),
            "reviews": archer_product.get("total_reviews", ""),
            "deal": deal,
            "source": "archer"
        }


class LevantaAPI:
    """Levanta Creator API client."""

    LEVANTA_BASE = "https://app.levanta.io/api/creator/v1"

    def __init__(self):
        self.api_key = os.environ.get("LEVANTA_API_KEY")

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    # ── BRANDS ───────────────────────────────────────────
    def get_brands(self, access_only=True, marketplace="amazon.com", limit=100):
        all_brands = []
        cursor = None
        pages = 0
        while pages < 50:
            params = {"limit": limit, "marketplace": marketplace}
            if access_only:
                params["access"] = "true"
            if cursor:
                params["cursor"] = cursor
            r = requests.get(f"{self.LEVANTA_BASE}/brands",
                headers=self._headers(), params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            brands = data.get('brands', [])
            all_brands.extend(brands)
            cursor = data.get('cursor')
            if not cursor or not brands:
                break
            pages += 1
        return {'brands': all_brands}

    # ── PRODUCTS ─────────────────────────────────────────
    def get_products(self, limit=100, cursor=None, marketplace="amazon.com"):
        params = {"limit": limit, "marketplace": marketplace}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{self.LEVANTA_BASE}/products",
            headers=self._headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_product_by_asin(self, asin, marketplace="amazon.com"):
        r = requests.get(f"{self.LEVANTA_BASE}/products/{asin}",
            headers=self._headers(),
            params={"marketplace": marketplace}, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ── LINKS ────────────────────────────────────────────
    def create_product_link(self, asin, source_id=None, marketplace="amazon.com"):
        """Create a tracked Levanta affiliate link for a given ASIN."""
        payload = {"asin": asin, "marketplace": marketplace}
        if source_id:
            payload["sourceId"] = source_id
        r = requests.post(f"{self.LEVANTA_BASE}/links",
            headers=self._headers(), json=payload, timeout=10)
        r.raise_for_status()
        return r.json()

    # ── DEALS ────────────────────────────────────────────
    def get_deals(self, limit=50):
        """Live deals feed — great for Steph's deals content."""
        r = requests.get(f"{self.LEVANTA_BASE}/deals",
            headers=self._headers(), params={"limit": limit}, timeout=15)
        r.raise_for_status()
        return r.json()

    # ── CPC CAMPAIGNS ────────────────────────────────────
    def get_cpc_campaigns(self, limit=50):
        r = requests.get(f"{self.LEVANTA_BASE}/cost-per-click-campaigns",
            headers=self._headers(), params={"limit": limit}, timeout=15)
        r.raise_for_status()
        return r.json()

    # ── REPORTS ──────────────────────────────────────────
    def get_reports(self, limit=100):
        r = requests.get(f"{self.LEVANTA_BASE}/reports",
            headers=self._headers(), params={"limit": limit}, timeout=15)
        r.raise_for_status()
        return r.json()

    # ── FORMAT FOR FRONTEND ──────────────────────────────
    def format_for_frontend(self, product, link_url=None):
        """Convert Levanta product to the same shape the UI expects."""
        commission = product.get("commission", 0)
        commission_str = f"{int(commission * 100)}%" if commission else ""
        pricing = product.get("pricing", {})
        price = pricing.get("price", "")
        return {
            "asin": product.get("asin", ""),
            "product_name": product.get("title") or product.get("name") or "",
            "company_name": product.get("brand") or product.get("brandName") or "",
            "price": f"${price}" if price else "",
            "commission_payout": commission_str,
            "image_encoded_string": product.get("image") or product.get("imageUrl") or "",
            "product_category": product.get("category") or product.get("productGroup") or "",
            "link": link_url or "",
            "source": "levanta",
            "marketplace": product.get("marketplace", "amazon.com"),
            "brand_id": product.get("brandId", ""),
            "deal": product.get("deal") or {}
        }

    def get_brand_lookup(self):
        """
        Fetch all Levanta brands and return a dict of brandId -> brand name.
        Products only carry a brandId UUID, so we need this to get human-readable names.
        Brands API returns: {brands: [{brandId: "uuid", brandName: "Brand Name", ...}]}
        """
        brand_lookup = {}
        try:
            data = self.get_brands(access_only=False)
            for b in data.get('brands', []):
                bid  = b.get('brandId', '')
                name = b.get('brandName', '')
                if bid and name:
                    brand_lookup[bid] = name
            logging.info(f'[LEVANTA] Brand lookup built: {len(brand_lookup)} brands')
        except Exception as e:
            logging.warning(f'[LEVANTA] Brand lookup failed: {e}')
        return brand_lookup

    def get_all_accessible_asins(self):
        """
        Page through all Levanta products and return a dict of
        asin -> {commission, commission_pct, title, brand} for accessible products only.
        Brand name is resolved via brandId → brands endpoint lookup.
        """
        brand_name_map = {}
        try:
            brands_data = self.get_brands(access_only=False)
            for b in brands_data.get('brands', []):
                brand_name_map[b.get('brandId', '')] = b.get('brandName', '')
            logging.info(f'[LEVANTA] Brand name map: {len(brand_name_map)} brands')
        except Exception as e:
            logging.warning(f'[LEVANTA] Brand lookup failed: {e}')

        asin_map = {}
        cursor = None
        pages = 0
        while pages < 200:
            data = self.get_products(limit=100, cursor=cursor)
            products = data.get('products', [])
            for p in products:
                if p.get('access') is True:
                    asin = p.get('asin')
                    if asin:
                        brand_name = brand_name_map.get(p.get('brandId'), '')
                        commission_val = p.get('commission', 0)
                        pricing = p.get('pricing', {})
                        asin_map[asin] = {
                            'commission':     commission_val,
                            'commission_pct': f"{int(commission_val * 100)}%" if commission_val else '',
                            'title':          p.get('title') or '',
                            'brand':          brand_name,
                            'imageUrl':       p.get('image') or '',
                            'category':       p.get('category') or '',
                            'price':          pricing.get('price', ''),
                            'rating':         p.get('rating') or '',
                            'ratingsTotal':   p.get('ratingsTotal') or 0,
                        }
            cursor = data.get('cursor')
            if not cursor or not products:
                break
            pages += 1
        logging.info(f'[LEVANTA] get_all_accessible_asins: {len(asin_map)} accessible products')
        return asin_map

    def search_products(self, query, limit=24):
        """
        Levanta doesn't have a search endpoint — pull products and filter locally.
        Returns products filtered by query match on title/brand/asin.
        Brand names are resolved via brandId lookup.
        """
        brand_lookup = self.get_brand_lookup()
        results = []
        cursor = None
        pages = 0
        while len(results) < limit * 3 and pages < 5:
            data = self.get_products(limit=100, cursor=cursor)
            products = data.get("products", [])
            for p in products:
                # Resolve brand name and attach it so format_for_frontend can use it
                brand_id   = p.get("brandId", "")
                brand_name = brand_lookup.get(brand_id, "")
                p["brand"]  = brand_name  # attach resolved name onto product dict
                title = (p.get("title") or p.get("name") or "").lower()
                asin  = (p.get("asin") or "").lower()
                if query.lower() in title or query.lower() in brand_name.lower() or query.lower() in asin:
                    results.append(p)
            cursor = data.get("cursor")
            if not cursor or not products:
                break
            pages += 1
        return results[:limit]


# Singletons — import these everywhere
archer_api = ArcherAPI()
levanta_api = LevantaAPI()


class URLGeniusAPI:
    """URLGenius deep link API client with registry-based deduplication."""
    BASE = "https://api.urlgeni.us/api/v2"
    REGISTRY_PATH = os.path.join(os.path.dirname(__file__), 'data', 'urlgenius_registry.json')

    def __init__(self):
        self.api_key = os.environ.get("URLGENIUS_API_KEY", "")
        self._registry = {}
        self._load_registry()

    # ── REGISTRY ──────────────────────────────────────────

    def _registry_key(self, destination_url, utm_source='', utm_medium='',
                      utm_campaign='', utm_content='', utm_term=''):
        return f"{destination_url}||{utm_source}|{utm_medium}|{utm_campaign}|{utm_content}|{utm_term}"

    def _load_registry(self):
        if os.path.exists(self.REGISTRY_PATH):
            try:
                with open(self.REGISTRY_PATH) as f:
                    self._registry = json.load(f)
                logging.info(f"[URLGENIUS] Registry loaded: {len(self._registry)} links")
            except Exception as e:
                logging.warning(f"[URLGENIUS] Registry load failed: {e}")
                self._registry = {}

    def _save_registry(self):
        try:
            os.makedirs(os.path.dirname(self.REGISTRY_PATH), exist_ok=True)
            with open(self.REGISTRY_PATH, 'w') as f:
                json.dump(self._registry, f, indent=2)
        except Exception as e:
            logging.warning(f"[URLGENIUS] Registry save failed: {e}")

    def seed_registry(self):
        """Fetch all existing URLgenius links and seed the registry file."""
        if not self.api_key:
            return 0
        try:
            data = self.list_links(limit=500)
            links = data.get('links', data if isinstance(data, list) else [])
            n = 0
            for link in links:
                dest = link.get('url', '')
                genius_url = link.get('genius_url', '')
                if dest and genius_url:
                    self._registry[dest] = {
                        'genius_url': genius_url,
                        'link_id': link.get('id'),
                        'affiliate_url': dest,
                    }
                    n += 1
            self._save_registry()
            logging.info(f"[URLGENIUS] Registry seeded: {n} links loaded")
            return n
        except Exception as e:
            logging.error(f"[URLGENIUS] Registry seed failed: {e}")
            return 0

    # ── HEADERS ───────────────────────────────────────────

    def _headers(self):
        return {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }

    # ── LINKS ─────────────────────────────────────────────

    def create_link(self, destination_url, utm_source=None, utm_medium=None,
                    utm_campaign=None, utm_content=None, utm_term=None,
                    force_new=False):
        """
        Create a URLGenius deep link. Checks registry first to avoid duplicates.
        Set force_new=True to always create a fresh link.
        """
        reg_key = self._registry_key(
            destination_url,
            utm_source or '', utm_medium or '',
            utm_campaign or '', utm_content or '', utm_term or ''
        )
        if not force_new and reg_key in self._registry:
            logging.info(f"[URLGENIUS] Registry hit: {reg_key[:60]}")
            return {'link': self._registry[reg_key], '_from_registry': True}

        payload = {"url": destination_url}
        utms = {}
        if utm_source:   utms["utm_source"]   = utm_source
        if utm_medium:   utms["utm_medium"]   = utm_medium
        if utm_campaign: utms["utm_campaign"] = utm_campaign
        if utm_content:  utms["utm_content"]  = utm_content
        if utm_term:     utms["utm_term"]     = utm_term
        if utms:
            payload["utm"] = utms

        r = requests.post(f"{self.BASE}/links", headers=self._headers(),
                          json=payload, timeout=10)
        r.raise_for_status()
        result = r.json()

        link_data = result.get('link', {})
        if link_data.get('genius_url'):
            self._registry[reg_key] = {
                'genius_url': link_data['genius_url'],
                'link_id': link_data.get('id'),
                'affiliate_url': destination_url,
            }
            self._save_registry()

        return result

    def list_links(self, limit=50):
        """List all created links."""
        r = requests.get(f"{self.BASE}/links", headers=self._headers(),
                         params={"limit": limit}, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_link_stats(self, link_id):
        """Fetch 30-day stats for a single link."""
        r = requests.get(f"{self.BASE}/links/{link_id}", headers=self._headers(),
                         timeout=10)
        r.raise_for_status()
        return r.json()

    def delete_link(self, link_id):
        """Remove a link."""
        r = requests.delete(f"{self.BASE}/links/{link_id}", headers=self._headers(),
                            timeout=10)
        r.raise_for_status()
        return r.status_code == 204


class ProductResolver:
    """Smart product resolution with CVR-based routing"""

    CVR_RULES = {
        'toys': 'walmart',
        'baby': 'walmart',
        'kids': 'walmart',
        'beauty': 'archer',
        'health': 'archer',
        'skincare': 'archer',
        'electronics': 'archer',
        'clothing': 'archer',
        'pets': 'archer',
        'home': 'archer',
        'outdoor': 'wayfair',
        'household': 'amazon',
        'essentials': 'amazon',
        'grocery': 'amazon',
        'food': 'amazon'
    }

    def __init__(self, hot_catalog: List[Dict]):
        self.hot_catalog = hot_catalog
        self.walmart_api = WalmartAPI()
        self.crawlbase_api = CrawlbaseAPI()
        self.impact_api = ImpactAPI()
        self.archer_api = archer_api

    def resolve(self, query: str, category: str = None, max_results: int = 3) -> List[Dict]:
        """
        Resolve products for a query using intelligent routing:
        1. Search Hot Score catalog first
        2. Search Archer catalog (matched ASINs with attribution links)
        3. Fall back to Walmart API
        4. Generate affiliate links for any unlinked results
        """
        results = []

        # Step 1: Hot Score catalog
        hot_matches = self._search_hot_catalog(query, category)
        results.extend(hot_matches)

        # Step 2: Archer catalog
        if len(results) < max_results:
            try:
                archer_matches = self.archer_api.search_catalog(
                    query, category, limit=max_results - len(results)
                )
                for p in archer_matches:
                    link_data = self.archer_api.generate_link(
                        p['asin'], label=f"chat-{category or 'general'}"
                    )
                    url = link_data.get('url') if link_data else None
                    results.append(self.archer_api.format_for_frontend(p, url))
            except Exception as e:
                logging.error(f"[ARCHER] Resolution error: {e}")

        # Step 3: Walmart API fallback
        if len(results) < max_results:
            preferred_retailer = self._get_preferred_retailer(category)

            if preferred_retailer == 'walmart':
                walmart_products = self.walmart_api.search(query, max_results - len(results))
                for product in walmart_products:
                    if product.get('url'):
                        product['link'] = self.impact_api.generate_walmart_link(
                            product['url'], product.get('sku'),
                            sub_id1='chat-recommendation', sub_id2=product.get('sku')
                        )
                results.extend(walmart_products)
            else:
                walmart_products = self.walmart_api.search(query, max_results - len(results))
                if walmart_products:
                    for product in walmart_products:
                        if product.get('url'):
                            product['link'] = self.impact_api.generate_walmart_link(
                                product['url'], product.get('sku'),
                                sub_id1='chat-recommendation', sub_id2=product.get('sku')
                            )
                    results.extend(walmart_products)
                else:
                    hot_fallback = self._search_hot_catalog(query, category)
                    results.extend(hot_fallback[:max_results - len(results)])

        # Fill any missing links
        for product in results:
            if not product.get('link'):
                if product.get('retailer') == 'Amazon' and product.get('asin'):
                    product['link'] = self.crawlbase_api.build_affiliate_link(product['asin'])
                elif product.get('retailer') == 'Walmart' and product.get('url'):
                    product['link'] = self.impact_api.generate_walmart_link(product['url'], product.get('sku'))

        return results[:max_results]

    def _search_hot_catalog(self, query: str, category: str = None) -> List[Dict]:
        """Search the Hot Score catalog with improved matching"""
        query_lower = query.lower()
        query_words = set(query_lower.split())
        matches = []

        for product in self.hot_catalog:
            score = 0

            if any(word in product['name'].lower() for word in query_words if len(word) > 2):
                score += 3

            if category and category.lower() in product.get('category', '').lower():
                score += 2
            elif any(word in product.get('category', '').lower() for word in query_words):
                score += 1

            if score > 0:
                matches.append((score, product))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [m[1] for m in matches]

    def _get_preferred_retailer(self, category: str) -> str:
        if not category:
            return 'walmart'
        return self.CVR_RULES.get(category, 'walmart')
