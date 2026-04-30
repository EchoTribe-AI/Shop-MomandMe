"""
ASIN extraction utility.
Handles raw ASINs, standard Amazon URLs, short Amazon links (a.co),
and URLGenius short links (go.urlgeni.us).
"""

import os
import re
import logging
import requests

_ASIN_RE = re.compile(r'[A-Z0-9]{10}')
_DP_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')


def extract_asin(input_str: str) -> 'str | None':
    """
    Return a 10-character ASIN from any of the supported input formats:
      1. Raw ASIN          "B0DKZTSB3K"
      2. Amazon URL        "https://www.amazon.com/dp/B0DKZTSB3K"
      3. Amazon long URL   "https://www.amazon.com/Title/dp/B0DKZTSB3K?ref=..."
      4. Amazon short URL  "https://a.co/d/XXXXXXX"  (follows redirect)
      5. URLGenius link    "https://go.urlgeni.us/XXXXX" (resolves via API)
    Returns None if extraction fails.
    """
    s = (input_str or '').strip()
    if not s:
        return None

    # 1. Raw ASIN — exactly 10 uppercase alphanumeric characters
    if re.fullmatch(r'[A-Z0-9]{10}', s):
        return s

    # 2 & 3. Standard Amazon URLs containing /dp/ or /gp/product/
    m = _DP_RE.search(s)
    if m:
        return m.group(1)

    # 4. Amazon short URLs (a.co and amzn.to) — follow redirect, then extract
    if 'a.co/' in s or 'amzn.to/' in s:
        try:
            r = requests.get(s, allow_redirects=True, timeout=10,
                             headers={'User-Agent': 'Mozilla/5.0'})
            final_url = r.url
            m = _DP_RE.search(final_url)
            if m:
                return m.group(1)
        except Exception as e:
            logging.warning(f'[ASIN] short URL redirect failed for {s}: {e}')
        return None

    # 5. URLGenius short link — call the links API to get destination URL
    if 'urlgeni.us/' in s:
        slug_m = re.search(r'urlgeni\.us/([^/?#]+)', s)
        if slug_m:
            slug = slug_m.group(1)
            api_key = os.environ.get('URLGENIUS_API_KEY', '')
            try:
                resp = requests.get(
                    f'https://api.urlgeni.us/api/v2/links/{slug}',
                    headers={'api-key': api_key},
                    timeout=10,
                )
                data = resp.json()
                dest = (data.get('link') or {}).get('url', '')
                m = _DP_RE.search(dest)
                if m:
                    return m.group(1)
            except Exception as e:
                logging.warning(f'[ASIN] URLGenius resolve failed for {slug}: {e}')
        return None

    return None
