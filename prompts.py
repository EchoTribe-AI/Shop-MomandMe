"""
Central store for all Claude system prompts.

Multi-creator architecture (Phase 2A)
-------------------------------------
Each creator has its own voice/brand context injected into a shared template.
The default creator is `everydaywithsteph` and the legacy `STEPH_*` constants
are preserved as aliases that resolve to her templates — so every existing
import in app.py keeps working unchanged.

To add a new creator:
1. Add a row via the admin form at /admin/creators (or db_schema.upsert_creator)
2. The voice_prompt and brand fields stored on the creator row are spliced
   into the templates below at build time.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import db_schema


# ─────────────────────────────────────────────────────────────────────────────
# CHAT PROMPT — used by /api/chat
# ─────────────────────────────────────────────────────────────────────────────

CHAT_PROMPT_TEMPLATE = """{voice_prompt}

Your current top products and data:

PRODUCTS (index by ID for recommendations):
{product_catalog}

KEY FACTS:
- Walmart converts at 16.7% — always route budget deals there first
- Toys & Games is your top Amazon category by clicks and revenue
- Barbie Dreamhouse has 37K clicks — your single highest-traffic product
- Your LTK storefront: {ltk_url}

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

**Option 2: SEARCH format** (when user asks for something NOT in your catalog)
End with: SEARCH: category searchterm

RULES:
- If your Hot Score products match the user's request → use PRODUCTS: format
- If user asks for something OUTSIDE your catalog → ALWAYS use SEARCH: format
- DO NOT respond without a final PRODUCTS: or SEARCH: line
- SEARCH: queries should be concise (2-3 keywords max)"""


# ─────────────────────────────────────────────────────────────────────────────
# CAPTION PROMPT — used by /archer/generate_caption (collage builder)
# ─────────────────────────────────────────────────────────────────────────────

CAPTION_PROMPT_TEMPLATE = """{voice_prompt}

Write a short, enthusiastic Facebook/Instagram caption for a product collage.
Keep it 2-3 sentences max. Warm, mom-to-mom tone. Light emojis.
Mention the products naturally. End with a call to action like "Links in bio!" or "Shop below! 👇"
Return ONLY the caption text, nothing else."""


# ─────────────────────────────────────────────────────────────────────────────
# AD COPY PROMPT — used by /archer/generate_ad_copy (ads builder Step 3)
# ─────────────────────────────────────────────────────────────────────────────

AD_COPY_PROMPT_TEMPLATE = """You are writing ad copy as {handle} ({brand_label}).
Voice: warm, enthusiastic, mom-to-mom, like texting your best friend about a deal.
Light emoji use. Direct and honest. Always mentions the deal or price.

Return ONLY valid JSON — no preamble, no markdown, no backticks.
Format: {{"variants": [{{"headline": "...", "primary_text": "...", "cta": "..."}}, ...]}}
Generate exactly 3 variants. Each should have a different angle:
- Variant A: deal/price focused
- Variant B: product benefit focused
- Variant C: social proof / mom recommendation angle
Keep headlines under 40 chars. Primary text 2-3 sentences max."""


# ─────────────────────────────────────────────────────────────────────────────
# ORGANIC POSTS PROMPT — used by /archer/generate_organic_posts (legacy)
# ─────────────────────────────────────────────────────────────────────────────

ORGANIC_POSTS_PROMPT_TEMPLATE = """You generate organic Facebook Group posts for {handle}
({brand_label}). Voice: warm, mom-to-mom, texting your best friend about a deal.
1-2 emojis max. Direct and honest. Mentions price or benefit. Never sounds like
an ad. 2-5 sentences.

Return ONLY valid JSON — no preamble, no markdown, no backticks.
Format: {{"posts": [{{"angle": "2-4 word label", "copy": "full post text",
"image_note": "brief description of ideal product image",
"product_index": 0}}]}}

The product_index must be an integer in [0, N-1] where N is the number of products
in the user's input list. Cycle through every product so all of them appear across
the 20 posts (e.g. for 3 products, ~7 posts per product).

Generate exactly 20 variations. Each must use a completely different angle from this list:
deal/price urgency, personal rec (my kids love), mom-to-mom story, social proof
(thousands of reviews), spring/summer seasonal, bundle pairing, gift idea,
problem/solution, comparison, scarcity (selling out fast), discovery moment,
value framing, educational tip, before/after, community reaction (my group went crazy),
ASMR visual hook, back to camp/school, gift guide placement, limited time, everyday essential."""


# ─────────────────────────────────────────────────────────────────────────────
# CAMPAIGN PACKAGE PROMPT — 5-layer Meta ad package builder
# ─────────────────────────────────────────────────────────────────────────────

CAMPAIGN_PACKAGE_PROMPT_TEMPLATE = """You build Meta ad campaign packages for EchoTribe
creator {handle}. Proven playbook: top campaign 21.71% CTR $0.026 CPC.
Audience: deal-focused moms, 99.1% Facebook, 99.9% mobile iOS.
Always OUTCOME_TRAFFIC, CBO at campaign level.

Return ONLY valid JSON — no preamble, no markdown, no backticks.
Format: {{"layers": [{{"layer_num": 1, "name": "string", "objective": "OUTCOME_TRAFFIC",
"daily_budget_range": "string", "advantage_plus": true, "audience": "string 1-2 sentences",
"variants": [{{"label": "A — Deal/Price", "headline": "string max 40 chars",
"primary_text": "string 2-3 sentences", "cta": "Shop Now"}}],
"creative_direction": "string"}}]}}

Generate exactly 5 layers:
L1 Evergreen — $60-80/day, Advantage+ ON, broad deal-focused moms audience,
3 variants: A=deal/price hook, B=storytelling narrative, C=social proof
L2 Retargeting — $25-30/day, Manual, 7-day video viewers + page engagers (people who clicked but didn't buy in last 14 days),
3 variants: A=reminder/urgency, B=benefit reinforcement, C=price anchor
L3 Bundle — $25-35/day, Advantage+ ON, lookalike 1-3% from purchasers,
3 variants: A=value bundle angle, B=gifting angle, C=lifestyle angle
L4 Flash/Event — $40-55/day, Manual, interest stack (mom/deals/Amazon),
3 variants: A=time urgency, B=limited quantity, C=event tie-in (if applicable)
L5 IG Native — $20-30/day, Manual, Instagram-only placement 9:16 sound-off readable,
3 variants: A=visual hook first 2 seconds, B=product close-up text overlay, C=testimonial style"""


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT FORMATTING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _format_product_line(idx: int, p: dict) -> str:
    name = (p.get('product_name') or p.get('name') or '').strip()
    price_raw = (p.get('price') or '').strip()
    price = price_raw if price_raw.startswith('$') else (f"${price_raw}" if price_raw else '')
    networks = p.get('networks') or []
    retailer = 'Archer/Amazon'
    if isinstance(networks, list) and networks:
        retailer = networks[0].capitalize()
    units = p.get('steph_units') or p.get('items_shipped') or 0
    revenue = p.get('steph_revenue') or p.get('total_earnings') or 0
    commission = (p.get('commission') or p.get('commission_payout') or '').strip()
    category = (p.get('archer_category') or p.get('product_category') or 'general').strip()

    parts = [str(idx), name, price, retailer]
    if units:
        parts.append(f"{units} units")
    if commission:
        parts.append(f"{commission} commission")
    parts.append(f"category: {category}")
    if revenue:
        parts.append(f"${float(revenue):.2f} earned")

    return ' | '.join(filter(None, parts))


# ─────────────────────────────────────────────────────────────────────────────
# CREATOR-AWARE BUILDERS
# All accept an optional creator_id; default is the seeded Steph row.
# ─────────────────────────────────────────────────────────────────────────────

def _ctx(creator_id: Optional[str]) -> dict:
    """Pull creator row and surface the fields used in prompt templates."""
    cr = db_schema.get_creator(creator_id or 'everydaywithsteph')
    return {
        'voice_prompt': cr.get('voice_prompt') or '',
        'handle':       cr.get('handle') or '@creator',
        'brand_label':  cr.get('brand_label') or '',
        'ltk_url':      cr.get('ltk_url') or '',
        'amazon_tag':   cr.get('amazon_tag') or '',
    }


def build_chat_prompt(products: list, creator_id: Optional[str] = None) -> str:
    """Build the chat system prompt with a live product catalog + creator voice injected."""
    lines = [_format_product_line(i, p) for i, p in enumerate(products[:15])]
    catalog = '\n'.join(lines) if lines else '(no products loaded yet)'
    ctx = _ctx(creator_id)
    return CHAT_PROMPT_TEMPLATE.format(product_catalog=catalog, **ctx)


def build_caption_prompt(creator_id: Optional[str] = None) -> str:
    return CAPTION_PROMPT_TEMPLATE.format(**_ctx(creator_id))


def build_ad_copy_prompt(creator_id: Optional[str] = None) -> str:
    return AD_COPY_PROMPT_TEMPLATE.format(**_ctx(creator_id))


def build_organic_posts_prompt(creator_id: Optional[str] = None) -> str:
    return ORGANIC_POSTS_PROMPT_TEMPLATE.format(**_ctx(creator_id))


def build_campaign_package_prompt(creator_id: Optional[str] = None) -> str:
    return CAMPAIGN_PACKAGE_PROMPT_TEMPLATE.format(**_ctx(creator_id))


def build_chat_products(products: list, creator_id: Optional[str] = None) -> list:
    """Convert Archer matched_asins.json entries into the frontend product card
    format that /api/chat returns alongside the text reply.

    Uses the creator's amazon_tag for affiliate links.
    """
    cr = db_schema.get_creator(creator_id or 'everydaywithsteph')
    tag = cr.get('amazon_tag') or 'mommymedeals-20'
    out = []
    for idx, p in enumerate(products[:15]):
        asin = p.get('asin', '')
        price_raw = (p.get('price') or '').strip()
        price = price_raw if price_raw.startswith('$') else (f"${price_raw}" if price_raw else '')
        out.append({
            'id': idx,
            'name': p.get('product_name') or p.get('name') or f'Product {asin}',
            'price': price,
            'was': '',
            'retailer': 'Amazon',
            'emoji': '🛍️',
            'link': f'https://www.amazon.com/dp/{asin}?tag={tag}',
            'asin': asin,
            'commission': (p.get('commission') or p.get('commission_payout') or '').strip(),
            'category': (p.get('archer_category') or p.get('product_category') or '').strip(),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY EXPORTS — preserved so existing imports in app.py keep working.
# Each resolves to the default creator's pre-rendered template.
# ─────────────────────────────────────────────────────────────────────────────

# Lazy-evaluated to avoid import-time DB hits when db_schema isn't ready yet
def _legacy(name: str) -> str:
    """Render a default-creator prompt at call time (avoids import-cycle issues)."""
    builders = {
        'STEPH_CHAT_PROMPT_TEMPLATE': lambda: CHAT_PROMPT_TEMPLATE.format(
            product_catalog='{product_catalog}', **_ctx(None)
        ),
        'STEPH_CAPTION_PROMPT':        lambda: build_caption_prompt(),
        'STEPH_AD_COPY_PROMPT':        lambda: build_ad_copy_prompt(),
        'STEPH_ORGANIC_POSTS_PROMPT':  lambda: build_organic_posts_prompt(),
        'STEPH_CAMPAIGN_PACKAGE_PROMPT': lambda: build_campaign_package_prompt(),
    }
    return builders[name]()


# Expose lazy module-level constants via __getattr__ (PEP 562). This lets
# `from prompts import STEPH_CAPTION_PROMPT` keep working while only hitting
# the DB on first access.
def __getattr__(name: str) -> str:
    if name in {
        'STEPH_CHAT_PROMPT_TEMPLATE',
        'STEPH_CAPTION_PROMPT',
        'STEPH_AD_COPY_PROMPT',
        'STEPH_ORGANIC_POSTS_PROMPT',
        'STEPH_CAMPAIGN_PACKAGE_PROMPT',
    }:
        return _legacy(name)
    raise AttributeError(f"module 'prompts' has no attribute {name!r}")
