"""
Crawlbase live price fallback.
Called only when Archer returns final_price == None or 0.
Requires CRAWLBASE_JS_TOKEN (JS token, not static — Amazon prices are JS-rendered).
"""

import os
import re
import logging
import requests


def get_live_price(asin: str) -> 'float | None':
    """
    Fetch the current Amazon price for asin via Crawlbase JS rendering.
    Returns a float (e.g. 29.99) or None if fetch/parse fails.
    """
    token = os.environ.get('CRAWLBASE_JS_TOKEN')
    if not token:
        logging.debug('[CRAWLBASE] CRAWLBASE_JS_TOKEN not set — skipping live price')
        return None

    params = {
        'token': token,
        'url': f'https://www.amazon.com/dp/{asin}',
        'ajax_wait': 'true',
        'page_wait': '2000',
    }
    try:
        resp = requests.get('https://api.crawlbase.com/', params=params, timeout=30)
        if resp.status_code != 200:
            logging.warning(f'[CRAWLBASE] Non-200 for {asin}: {resp.status_code}')
            return None
        return _parse_price(resp.text)
    except Exception as e:
        logging.warning(f'[CRAWLBASE] Price fetch failed for {asin}: {e}')
        return None


def _parse_price(html: str) -> 'float | None':
    patterns = [
        # Whole + fraction spans (most reliable)
        (r'<span[^>]+class="[^"]*a-price-whole[^"]*">(\d[\d,]*)<',
         r'<span[^>]+class="[^"]*a-price-fraction[^"]*">(\d+)<'),
        # JSON price amount
        (r'"priceAmount"\s*:\s*(\d+(?:\.\d+)?)', None),
        # priceblock IDs
        (r'id="priceblock_ourprice"[^>]*>\s*\$?([\d,]+\.?\d*)', None),
        (r'id="priceblock_dealprice"[^>]*>\s*\$?([\d,]+\.?\d*)', None),
        # apex price
        (r'id="apex_desktop_[^"]*"[^>]*>.*?\$\s*([\d,]+\.?\d*)', None),
    ]

    for whole_pat, frac_pat in patterns:
        if frac_pat:
            m_whole = re.search(whole_pat, html)
            m_frac = re.search(frac_pat, html)
            if m_whole and m_frac:
                try:
                    return float(f"{m_whole.group(1).replace(',', '')}.{m_frac.group(1)}")
                except ValueError:
                    continue
        else:
            m = re.search(whole_pat, html, re.DOTALL)
            if m:
                try:
                    return float(m.group(1).replace(',', ''))
                except ValueError:
                    continue

    return None
