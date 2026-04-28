"""
LinkBuilder abstraction — pluggable affiliate-link generation per retailer.

Phase 2A introduces this layer so we can add Walmart (via Impact API) in Phase 2C
without changing any call sites. Today the only working backend is Archer
(URLGenius wrap → Amazon affiliate URL); ImpactStub raises NotImplementedError.

Usage:
    from link_builder import build_smart_link
    link = build_smart_link(
        item_id='B0CXXXXXXX',
        network='amazon',
        utm={'source': 'fb-ad', 'medium': 'paid_social',
             'campaign': 'summer-evergreen', 'content': 'ad2a_static'},
        creator_id='everydaywithsteph',
    )
    # link → {'genius_url', 'affiliate_url', 'label', 'urlgenius', 'network'}

Why a registry rather than direct dispatch:
- Single source of truth for which networks are supported
- New retailers (Walmart Impact, etc.) plug in by registering a class
- Existing _make_smart_link() in app.py still works — it now delegates here
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Optional, Protocol

import db_schema


# ─────────────────────────────────────────────────────────────────────────────
# Network-derived utm_content fallback
# (caller-supplied utm.content always wins; this is the default when absent)
# ─────────────────────────────────────────────────────────────────────────────
NETWORK_CONTENT_DEFAULTS = {
    'amazon':         'amazon-assoc',
    'archer':         'archer',
    'levanta':        'levanta',
    'walmart_impact': 'walmart-impact',
}


class LinkBuilder(Protocol):
    """Protocol every retailer-specific link builder must satisfy."""

    network_id: str

    def build(
        self,
        item_id: str,
        utm: dict,
        creator: dict,
    ) -> dict:
        """Return {'genius_url', 'affiliate_url', 'label', 'urlgenius', 'network'}."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Archer (Amazon via URLGenius) — production backend
# ─────────────────────────────────────────────────────────────────────────────
class ArcherURLGenius:
    network_id = 'archer'

    def build(self, item_id: str, utm: dict, creator: dict) -> dict:
        from product_api import ArcherAPI, URLGeniusAPI

        amazon_tag = (
            creator.get('amazon_tag')
            or os.environ.get('AMAZON_AFFILIATE_TAG', 'mommymedeals-20')
        )
        affiliate_url = f'https://www.amazon.com/dp/{item_id}?tag={amazon_tag}'

        # Try to upgrade to a real Archer attribution link
        try:
            a = ArcherAPI()
            label_archer = f"{creator.get('id', 'creator')}-archer-{item_id.lower()}-{int(time.time())}"
            result = a.generate_link(item_id, label=label_archer)
            if result:
                affiliate_url = (
                    result.get('attribution_link')
                    or result.get('url')
                    or result.get('link')
                    or affiliate_url
                )
        except Exception as e:
            logging.warning(f'[LINK_BUILDER:archer] Archer link failed for {item_id}: {e}')

        utm_source   = utm.get('source')   or 'fb-group'
        utm_medium   = utm.get('medium')   or 'organic'
        utm_campaign = utm.get('campaign') or ''
        utm_term     = utm.get('term')     or ''
        utm_content  = (
            utm.get('content')
            or NETWORK_CONTENT_DEFAULTS.get('archer')
        )

        mmdd = datetime.now().strftime('%m%d')
        link_label = f'{utm_source}_{utm_medium}_{utm_campaign}_{mmdd}'

        ug = URLGeniusAPI()
        if not ug.api_key:
            return {
                'genius_url':    affiliate_url,
                'affiliate_url': affiliate_url,
                'label':         link_label,
                'urlgenius':     False,
                'network':       'archer',
            }

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
            link_obj = ug_result.get('link', {}) if isinstance(ug_result, dict) else {}
            genius_url = (
                link_obj.get('genius_url')
                if isinstance(link_obj, dict)
                else None
            ) or affiliate_url
            return {
                'genius_url':    genius_url,
                'affiliate_url': affiliate_url,
                'label':         link_label,
                'urlgenius':     True,
                'network':       'archer',
            }
        except Exception as e:
            logging.warning(f'[LINK_BUILDER:archer] URLGenius failed for {item_id}: {e}')
            return {
                'genius_url':    affiliate_url,
                'affiliate_url': affiliate_url,
                'label':         link_label,
                'urlgenius':     False,
                'network':       'archer',
            }


# ─────────────────────────────────────────────────────────────────────────────
# Impact (Walmart) — placeholder for Phase 2C
# ─────────────────────────────────────────────────────────────────────────────
class ImpactStub:
    network_id = 'walmart_impact'

    def build(self, item_id: str, utm: dict, creator: dict) -> dict:
        raise NotImplementedError(
            "Walmart Impact API integration ships in Phase 2C — "
            "set up your IMPACT_API_KEY and replace this stub."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────
_REGISTRY: dict[str, LinkBuilder] = {
    'archer':         ArcherURLGenius(),
    'amazon':         ArcherURLGenius(),  # alias — Amazon goes through Archer
    'walmart_impact': ImpactStub(),
}


def get_builder(network: str) -> LinkBuilder:
    builder = _REGISTRY.get((network or 'amazon').lower())
    if builder is None:
        raise ValueError(f"Unknown network '{network}'. Available: {list(_REGISTRY)}")
    return builder


def register(network: str, builder: LinkBuilder) -> None:
    """Public hook for tests / future networks."""
    _REGISTRY[network.lower()] = builder


def build_smart_link(
    item_id: str,
    network: str = 'amazon',
    utm: Optional[dict] = None,
    creator_id: Optional[str] = None,
) -> dict:
    """One-shot builder. Pulls the creator row, dispatches to the right backend."""
    creator = db_schema.get_creator(creator_id or 'everydaywithsteph')
    return get_builder(network).build(item_id, utm or {}, creator)
