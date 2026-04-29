"""
Campaign Builder v3 — produces spec-compliant Campaign Build Packages.

Reference: Campaign_Build_Package_Spec — defines the JSON shape that Claude +
Ryze MCP consume to build campaigns/ad sets/ads in Meta Ads.

Asset / Copy / CTA / Final Ad model (Q2 decision)
-------------------------------------------------
Each package targets ONE thing (asin / collection / boosted post). Within a
package, all selected layers SHARE one asset (image/video URL or boosted
post) but each layer has UNIQUE COPY tailored to its audience. The
spec-compliant `creative.assets[]` is built so each layer's `creative_ref`
points to the same image_url/video_url but distinct headline/body/description.

  asset      = image_url | video_url | boosted post object_story_id
  copy       = layer-specific (headline, body, description, CTA verb)
  CTA link   = destination_url (Amazon affiliate or shop landing page)
               + UTM parameters unique to (campaign × layer × creative_ref)
  final_ad   = asset + layer_copy + cta_link

Python module — no Flask, no DB. Pure functions returning dicts. The Flask
routes in app.py handle persistence into campaigns_v3.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode


# ─────────────────────────────────────────────────────────────────────────────
# Spec-defined defaults (Section "Defaults (Applied Unless Overridden)")
# Per-creator overrides via creators.defaults_json take precedence.
# ─────────────────────────────────────────────────────────────────────────────
SPEC_DEFAULTS: dict[str, Any] = {
    'daily_budget':         2500,                       # $25.00 in cents
    'budget_type':          'CBO',
    'objective':            'OUTCOME_TRAFFIC',
    'optimization_goal':    'LANDING_PAGE_VIEWS',
    'bid_strategy':         'LOWEST_COST_WITHOUT_CAP',
    'geo':                  ['US'],
    'age_min':              24,
    'age_max':              65,
    'gender':               0,
    'audience_type':        'advantage_plus',
    'publisher_platforms':  ['facebook', 'instagram'],
    'status':               'PAUSED',
    'currency':             'USD',
}

# Layer registry — names + budget-type defaults per spec (Section "Layer IDs").
# Each entry also carries a creative-direction hint Claude uses when generating
# layer-specific copy.
LAYER_REGISTRY: dict[str, dict] = {
    'L1': {
        'name': 'Evergreen', 'budget_type': 'CBO',
        'audience_default': 'advantage_plus',
        'copy_angle': 'broad deal-focused proof — what / why / price',
    },
    'L2': {
        'name': 'Story / Parent Voice', 'budget_type': 'CBO',
        'audience_default': 'advantage_plus',
        'copy_angle': 'first-person mom anecdote — 2-3 sentences, conversational',
    },
    'L3': {
        'name': 'Retargeting', 'budget_type': 'ABO',
        'audience_default': 'retargeting',
        'copy_angle': 'reminder/urgency for people who already engaged',
    },
    'L4': {
        'name': 'Bundle / Promo', 'budget_type': 'CBO',
        'audience_default': 'advantage_plus',
        'copy_angle': 'value framing — multi-product worth or stacking the deal',
    },
    'L5': {
        'name': 'Flash / Event', 'budget_type': 'CBO',
        'audience_default': 'advantage_plus',
        'copy_angle': 'time-bound urgency — limited quantity or end-of-sale',
    },
    'L6': {
        'name': 'IG Native (Reels)', 'budget_type': 'CBO',
        'audience_default': 'advantage_plus',
        'copy_angle': 'IG-native, sound-off readable, hook in first 2 seconds',
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Slug helpers
# ─────────────────────────────────────────────────────────────────────────────
def _slug(s: str, max_len: int = 30) -> str:
    s = (s or '').lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s[:max_len] or 'item'


def _layer_slug(layer_id: str) -> str:
    """L1 → 'evergreen', L2 → 'story', etc. From LAYER_REGISTRY name."""
    name = LAYER_REGISTRY.get(layer_id, {}).get('name', layer_id)
    # Take first word, lowercase
    first = name.split()[0].lower()
    return re.sub(r'[^a-z0-9]+', '', first) or layer_id.lower()


# ─────────────────────────────────────────────────────────────────────────────
# UTM auto-generation per UTM_Schema_Reference.md (Section "Full Examples")
# ─────────────────────────────────────────────────────────────────────────────
def auto_generate_utms(
    brand_slug: str,
    product_slug: str,
    layer_id: str,
    creative_ref: str,
    package_type: str,
    is_collection: bool = False,
    audience_type: str = 'advantage_plus',
) -> dict:
    """Builds the canonical UTM bundle for a layer×creative_ref ad."""
    layer_name_slug = _layer_slug(layer_id)
    if package_type == 'boost_post':
        utm_medium = 'boosted_post'
        campaign = f"{brand_slug}_{product_slug}_boost"
        content = f"boosted_{product_slug}_{creative_ref.lower()}"
    else:
        utm_medium = 'paid_social'
        campaign = f"{brand_slug}_{product_slug}_{layer_name_slug}"
        suffix = '_collection' if is_collection else ''
        content = f"{creative_ref.lower()}_{layer_name_slug}_static{suffix}"

    return {
        'utm_source':   'facebook',
        'utm_medium':   utm_medium,
        'utm_campaign': campaign,
        'utm_content':  content,
        'utm_term':     audience_type,
    }


def append_utms_to_url(url: str, utms: dict) -> str:
    """Append UTM params to a destination URL (preserves existing query)."""
    if not url:
        return url
    sep = '&' if '?' in url else '?'
    return url + sep + urlencode({k: v for k, v in utms.items() if v})


# ─────────────────────────────────────────────────────────────────────────────
# Brand slug resolution
# ─────────────────────────────────────────────────────────────────────────────
def resolve_brand_slug(creator: dict, product: dict | None = None) -> str:
    """Use product brand if available, else fall back to creator handle."""
    if product:
        b = (product.get('brand') or product.get('company_name') or '').strip()
        if b:
            return _slug(b, 20)
    handle = (creator.get('handle') or creator.get('id') or '').strip()
    return _slug(handle.replace('@', ''), 20) or 'creator'


def resolve_product_slug(target: dict) -> str:
    """Generate a product_slug from the target context."""
    if target.get('kind') == 'collection':
        return _slug(target.get('value') or 'collection', 30)
    name = target.get('product_name') or target.get('value') or 'product'
    return _slug(name, 30)


# ─────────────────────────────────────────────────────────────────────────────
# Defaults merging
# ─────────────────────────────────────────────────────────────────────────────
def merged_defaults(
    creator: dict | None = None,
    overrides: dict | None = None,
) -> dict:
    """Merge spec defaults <- creator defaults <- per-package overrides."""
    out = dict(SPEC_DEFAULTS)
    if creator and creator.get('defaults_json'):
        try:
            cd = json.loads(creator['defaults_json'])
            if isinstance(cd, dict):
                out.update({k: v for k, v in cd.items() if v is not None})
        except (json.JSONDecodeError, TypeError):
            pass
    if overrides:
        out.update({k: v for k, v in overrides.items() if v is not None})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Creative asset construction
# ─────────────────────────────────────────────────────────────────────────────
def build_creatives(
    asset_url: str,
    asset_type: str,                  # 'static_image' | 'video'
    layer_copies: list[dict],         # [{layer_id, headline, body, description, cta, thumbnail_url?}]
    brand_slug: str,
    product_slug: str,
    package_type: str,
    is_collection: bool = False,
    utm_auto: bool = True,
) -> tuple[list[dict], dict]:
    """Build the spec's creative.assets[] array + a layer→creative_ref map.

    Each layer gets its own creative_ref but shares the same asset_url
    (per Q2 — shared asset, unique copy).

    Returns: (creative_assets_list, {layer_id: creative_ref})
    """
    assets: list[dict] = []
    layer_creative_map: dict[str, str] = {}

    for i, lc in enumerate(layer_copies):
        layer_id = lc.get('layer_id') or f'L{i+1}'
        creative_ref = f'AD_{layer_id}'        # Stable per-layer ref (e.g. AD_L1)
        layer_creative_map[layer_id] = creative_ref

        utms = auto_generate_utms(
            brand_slug, product_slug, layer_id, creative_ref,
            package_type, is_collection,
            audience_type=lc.get('audience_type', 'advantage_plus'),
        ) if utm_auto else {}

        asset_obj: dict[str, Any] = {
            'creative_ref':    creative_ref,
            'headline':        lc.get('headline', '')[:40],
            'body':            lc.get('body', ''),
            'description':     lc.get('description', ''),
            'call_to_action':  lc.get('cta', 'SHOP_NOW'),
            'utm_content':     utms.get('utm_content', ''),
        }
        if asset_type == 'video':
            asset_obj['video_url'] = asset_url
            if lc.get('thumbnail_url'):
                asset_obj['thumbnail_url'] = lc['thumbnail_url']
        else:
            asset_obj['image_url'] = asset_url

        assets.append(asset_obj)

    return assets, layer_creative_map


# ─────────────────────────────────────────────────────────────────────────────
# Layer construction
# ─────────────────────────────────────────────────────────────────────────────
def build_layers(
    selected_layer_ids: list[str],
    layer_creative_map: dict[str, str],
    defaults: dict,
    layer_overrides: dict | None = None,    # {layer_id: {daily_budget, audience_type, ...}}
) -> list[dict]:
    """Build the spec's layers[] array."""
    layer_overrides = layer_overrides or {}
    out: list[dict] = []
    for lid in selected_layer_ids:
        meta = LAYER_REGISTRY.get(lid)
        if not meta:
            continue
        per = layer_overrides.get(lid, {}) or {}
        layer = {
            'layer_id':      lid,
            'name':          meta['name'],
            'daily_budget':  per.get('daily_budget', defaults['daily_budget']),
            'audience_type': per.get('audience_type', meta['audience_default']),
            'creative_ref':  layer_creative_map.get(lid, f'AD_{lid}'),
        }
        # ABO layers ride budget at the ad set level (per spec)
        if meta['budget_type'] == 'ABO':
            layer['budget_type'] = 'ABO'
        # Retargeting requires a source
        if layer['audience_type'] == 'retargeting':
            layer['retargeting_source'] = per.get(
                'retargeting_source', 'page_engagers_14d'
            )
        out.append(layer)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Package builders
# ─────────────────────────────────────────────────────────────────────────────
def build_new_campaign_package(
    creator: dict,
    target: dict,                     # {kind: 'asin'|'collection', value, product_name?, ...}
    selected_layer_ids: list[str],
    asset_url: str,
    asset_type: str,                  # 'static_image' | 'video'
    layer_copies: list[dict],         # one per layer_id with headline/body/...
    destination_url: str | None = None,
    defaults_override: dict | None = None,
    layer_overrides: dict | None = None,
    utm_auto: bool = True,
) -> dict:
    """Build a spec-compliant `new_campaign` package."""
    if not creator.get('id'):
        raise ValueError('creator.id is required')
    if not creator.get('meta_ad_account_id'):
        raise ValueError('creator.meta_ad_account_id is required')
    if not creator.get('fb_page_id'):
        raise ValueError(
            'creator.fb_page_id is required — set it on /admin/creators'
        )
    if not selected_layer_ids:
        raise ValueError('At least one layer_id must be selected')
    if not asset_url:
        raise ValueError('asset_url is required (image or video URL)')
    if not layer_copies:
        raise ValueError('layer_copies must contain entries for each selected layer')

    is_collection = target.get('kind') == 'collection'
    brand_slug = resolve_brand_slug(creator, target.get('product'))
    product_slug = resolve_product_slug(target)
    product_name = (
        target.get('product_name')
        or (target.get('product') or {}).get('product_name')
        or target.get('value')
    )

    if not destination_url:
        destination_url = _resolve_destination_url(creator, target)

    defaults = merged_defaults(creator, defaults_override)

    creative_assets, layer_creative_map = build_creatives(
        asset_url=asset_url,
        asset_type=asset_type,
        layer_copies=layer_copies,
        brand_slug=brand_slug,
        product_slug=product_slug,
        package_type='new_campaign',
        is_collection=is_collection,
        utm_auto=utm_auto,
    )

    layers = build_layers(
        selected_layer_ids, layer_creative_map, defaults, layer_overrides,
    )

    package = {
        'package_type':    'new_campaign',
        'ad_account_id':   creator['meta_ad_account_id'],
        'page_id':         creator['fb_page_id'],
        'brand':           brand_slug,
        'product':         product_name,
        'product_slug':    product_slug,
        'destination_url': destination_url,
        'layers':          layers,
        'creative': {
            'type':   asset_type,
            'assets': creative_assets,
        },
        'utm_auto':        bool(utm_auto),
    }
    if defaults_override:
        package['defaults_override'] = defaults_override
    return package


def build_boost_post_package(
    creator: dict,
    meta_post_id: str,
    boost_overrides: dict | None = None,
    product_slug: str = 'post',
    brand_slug: str | None = None,
    utm_auto: bool = True,
) -> dict:
    """Build a spec-compliant `boost_post` package."""
    if not creator.get('id'):
        raise ValueError('creator.id is required')
    if not creator.get('meta_ad_account_id'):
        raise ValueError('creator.meta_ad_account_id is required')
    if not creator.get('fb_page_id'):
        raise ValueError(
            'creator.fb_page_id is required — set it on /admin/creators'
        )
    if not meta_post_id:
        raise ValueError('meta_post_id is required')

    page_id = creator['fb_page_id']
    # Spec format expects either a bare post_id or "<page_id>_<post_id>".
    if '_' not in meta_post_id:
        meta_post_id = f'{page_id}_{meta_post_id}'

    brand_slug = brand_slug or resolve_brand_slug(creator)
    defaults = merged_defaults(creator, boost_overrides)

    boost: dict[str, Any] = {
        'daily_budget':   defaults['daily_budget'],
        'duration_days':  (boost_overrides or {}).get('duration_days', 7),
        'audience_type':  defaults['audience_type'],
        'geo':            defaults['geo'],
        'age_min':        defaults['age_min'],
        'age_max':        defaults['age_max'],
    }
    if utm_auto:
        utms = auto_generate_utms(
            brand_slug, product_slug, 'L1', 'AD_BOOST',
            'boost_post',
        )
        boost['utm_content'] = utms['utm_content']
        boost['utm_campaign'] = utms['utm_campaign']

    return {
        'package_type':  'boost_post',
        'ad_account_id': creator['meta_ad_account_id'],
        'page_id':       page_id,
        'post_id':       meta_post_id,
        'boost':         boost,
        'utm_auto':      bool(utm_auto),
    }


def _resolve_destination_url(creator: dict, target: dict) -> str:
    """Pick the destination URL for the package based on target kind."""
    kind = target.get('kind')
    val = target.get('value', '')
    shop_subdomain = os.environ.get('SHOP_SUBDOMAIN', 'shop.echotribe.ai')
    if kind == 'collection':
        return f'https://{shop_subdomain}/{val}'
    if kind == 'asin':
        tag = creator.get('amazon_tag') or 'mommymedeals-20'
        return f'https://www.amazon.com/dp/{val}?tag={tag}'
    return val or ''


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────
def validate_package(pkg: dict) -> list[str]:
    """Return a list of human-readable validation errors (empty list = OK)."""
    errors: list[str] = []
    if pkg.get('package_type') not in ('new_campaign', 'boost_post'):
        errors.append("package_type must be 'new_campaign' or 'boost_post'")
        return errors
    for required in ('ad_account_id', 'page_id'):
        if not pkg.get(required):
            errors.append(f'Missing required field: {required}')

    if pkg['package_type'] == 'new_campaign':
        if not pkg.get('destination_url'):
            errors.append('Missing destination_url')
        layers = pkg.get('layers') or []
        if not layers:
            errors.append('No layers defined')
        creative = pkg.get('creative') or {}
        assets = creative.get('assets') or []
        if not assets:
            errors.append('No creative assets defined')
        creative_refs = {a.get('creative_ref') for a in assets}
        for layer in layers:
            ref = layer.get('creative_ref')
            if ref not in creative_refs:
                errors.append(
                    f"Layer {layer.get('layer_id')} references unknown "
                    f"creative_ref '{ref}'"
                )
            if not layer.get('daily_budget'):
                errors.append(f"Layer {layer.get('layer_id')} missing daily_budget")
        for a in assets:
            if not a.get('headline'):
                errors.append(f"Creative {a.get('creative_ref')} missing headline")
            if not (a.get('image_url') or a.get('image_hash')
                    or a.get('video_url') or a.get('video_id')):
                errors.append(
                    f"Creative {a.get('creative_ref')} missing asset URL/ID"
                )
    elif pkg['package_type'] == 'boost_post':
        if not pkg.get('post_id'):
            errors.append('Missing post_id (Meta post id)')
        boost = pkg.get('boost') or {}
        if not boost.get('daily_budget'):
            errors.append('boost.daily_budget is required')

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Ryze MCP prompt rendering
# ─────────────────────────────────────────────────────────────────────────────
def render_ryze_prompt(pkg: dict, creator: dict | None = None) -> str:
    """Render a paste-ready prompt for the Ryze MCP. Includes the full JSON
    block plus the build sequence the spec defines."""
    handle = creator.get('handle', '@creator') if creator else '@creator'
    if pkg['package_type'] == 'new_campaign':
        steps = (
            "1. Upload any image_url / video_url assets → resolve image_hash / video_id\n"
            "2. For each layer: meta_create_campaign (PAUSED)\n"
            "3. For each layer: meta_create_adset with targeting\n"
            "4. For each layer: meta_create_ad_creative using resolved hashes\n"
            "5. For each layer: meta_create_ad linking creative to ad set\n"
            "6. Return campaign IDs, ad set IDs, ad IDs, and resolved UTM strings"
        )
    else:
        steps = (
            "1. Resolve post_id to object_story_id\n"
            "2. meta_create_campaign (PAUSED, OUTCOME_TRAFFIC)\n"
            "3. meta_create_adset with boost targeting\n"
            "4. meta_create_ad_creative using object_story_id\n"
            "5. meta_create_ad linking creative to ad set\n"
            "6. Return campaign/ad set/ad IDs and UTM strings"
        )
    return (
        f"Use the Ryze MCP connected to {handle}'s Meta account ({pkg.get('ad_account_id','?')}) "
        f"to build the Campaign Build Package below. Treat all campaigns as PAUSED until I confirm.\n\n"
        f"BUILD SEQUENCE\n{steps}\n\n"
        f"CAMPAIGN BUILD PACKAGE (JSON)\n```json\n"
        f"{json.dumps(pkg, indent=2)}\n```\n"
        f"Confirm each created entity with its ID."
    )
