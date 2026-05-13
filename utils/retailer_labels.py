"""Shared retailer-aware label helpers for public shop surfaces.

A single source of truth for the retailer name, shop CTA, and missing-price
placeholder that appears on landing pages, the trends page, the social posts
feed, and the collection editor.
"""
from __future__ import annotations

from typing import Any

WALMART = "walmart"
AMAZON = "amazon"


def retailer_key(product: Any) -> str:
    """Return 'walmart', 'amazon', or '' for an item-like dict."""
    if not isinstance(product, dict):
        return ""
    for field in ("network", "retailer", "retailer_name"):
        value = str(product.get(field) or "").strip().lower()
        if value in (WALMART, AMAZON):
            return value
    return ""


def retailer_label(product: Any) -> str:
    """Return the human-facing retailer label."""
    key = retailer_key(product)
    if key == WALMART:
        return "Walmart"
    if key == AMAZON:
        return "Amazon"
    return ""


def shop_cta(product: Any) -> str:
    """Return the shop button label for a single product card."""
    label = retailer_label(product)
    if label:
        return f"Shop {label} →"
    return "Shop Now →"


def price_placeholder(product: Any) -> str:
    """Return text to show when a product has no price."""
    label = retailer_label(product)
    if label:
        return f"See price at {label}"
    return "See price"


def collection_retailer(products: list[Any] | None) -> str:
    """Return the dominant retailer across a product list ('' if mixed/none)."""
    seen: set[str] = set()
    for product in products or []:
        key = retailer_key(product)
        if key:
            seen.add(key)
    if len(seen) == 1:
        return next(iter(seen))
    return ""  # mixed or unknown


ANGLE_LABELS = {
    "problem_solve": "Helpful Find",
    "problem-solve": "Helpful Find",
    "gift_idea": "Gift Pick",
    "gift-idea": "Gift Pick",
    "nostalgia": "Nostalgia Pick",
    "deal_price": "Deal Pick",
    "deal-price": "Deal Pick",
    "mom_rec": "Mom-Tested",
    "mom-rec": "Mom-Tested",
    "social_proof": "Crowd Favorite",
    "social-proof": "Crowd Favorite",
    "seasonal": "Seasonal Pick",
    "scarcity": "Limited Find",
}


def angle_label(angle: Any) -> str:
    """Return a friendly chip label for an internal post angle, or ''."""
    key = str(angle or "").strip().lower()
    return ANGLE_LABELS.get(key, "")


def collection_cta_default(products: list[Any] | None) -> str:
    """Default CTA copy for a collection editor draft."""
    key = collection_retailer(products)
    if key == WALMART:
        return "Shop the Walmart finds"
    if key == AMAZON:
        return "Shop the Amazon finds"
    return "Shop these finds"
