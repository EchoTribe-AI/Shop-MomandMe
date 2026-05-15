"""Walmart storefront product metadata enrichment.

This module updates display metadata only. It intentionally preserves existing
Walmart affiliate, Impact, URLGenius, and shop URLs exactly as stored.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import db_schema


WALMART_NETWORK_VALUES = {"walmart", "walmart_impact"}


def _connect():
    return db_schema._connect()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def price_display(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        text = _clean(value)
        return text if text.startswith("$") else text
    return f"${number:.2f}"


def is_walmart_product(product: dict[str, Any]) -> bool:
    fields = (
        product.get("network"),
        product.get("retailer"),
        product.get("retailer_name"),
    )
    return any(_clean(field).lower() in WALMART_NETWORK_VALUES for field in fields)


def sku_from_product(product: dict[str, Any]) -> str:
    return _clean(product.get("asin") or product.get("sku") or product.get("item_id"))


def _cached_walmart_product(sku: str) -> dict[str, Any]:
    if not sku:
        return {}
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM walmart_products WHERE sku = ?",
            (sku,),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        conn.close()


def _normalize_cached(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "product_name": row.get("product_title") or row.get("item_name") or "",
        "brand": row.get("brand") or "",
        "category": row.get("category_list") or row.get("taxonomy") or "",
        "image_encoded_string": row.get("image_url") or "",
        "current_price": row.get("current_price"),
        "price_display": row.get("price_display") or price_display(row.get("current_price")),
        "availability": row.get("availability") or "",
        "rating": row.get("rating"),
        "review_count": row.get("review_count"),
        "canonical_url": row.get("canonical_url") or "",
    }


def _normalize_live(item: dict[str, Any]) -> dict[str, Any]:
    if not item:
        return {}
    raw_price = item.get("price") or item.get("salePrice") or item.get("current_price")
    price_value = _to_float(raw_price)
    return {
        "product_name": item.get("name") or item.get("title") or "",
        "brand": item.get("brand") or "",
        "category": item.get("category") or item.get("categoryPath") or "",
        "image_encoded_string": item.get("imageUrl") or item.get("image") or item.get("largeImage") or item.get("mediumImage") or "",
        "current_price": price_value,
        "price_display": item.get("price_display") or price_display(raw_price),
        "availability": item.get("availability") or item.get("stock") or "",
        "rating": item.get("rating"),
        "review_count": item.get("review_count") or item.get("numReviews") or item.get("customerRatingCount"),
        "canonical_url": item.get("url") or item.get("productUrl") or "",
    }


def _merge_metadata(base: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    if not meta:
        return out

    if meta.get("price_display"):
        out["price"] = meta["price_display"]
        out["price_display"] = meta["price_display"]
    if meta.get("current_price") not in (None, ""):
        out["current_price"] = meta["current_price"]
    if meta.get("availability"):
        out["availability"] = meta["availability"]
    if meta.get("rating") not in (None, ""):
        out["rating"] = meta["rating"]
    reviews = _to_int(meta.get("review_count"))
    if reviews is not None:
        out["review_count"] = reviews

    if (not _clean(out.get("product_name")) or out.get("product_name") == sku_from_product(out)) and meta.get("product_name"):
        out["product_name"] = meta["product_name"]
    if not _clean(out.get("company_name") or out.get("brand")) and meta.get("brand"):
        out["company_name"] = meta["brand"]
        out["brand"] = meta["brand"]
    elif meta.get("brand") and not _clean(out.get("brand")):
        out["brand"] = meta["brand"]
    if not _clean(out.get("category")) and meta.get("category"):
        out["category"] = meta["category"]
    if not _clean(out.get("image_encoded_string")) and meta.get("image_encoded_string"):
        out["image_encoded_string"] = meta["image_encoded_string"]

    return out


def enrich_product_payload(
    product: dict[str, Any],
    *,
    fetch_live: bool = True,
    walmart_api: Any | None = None,
) -> dict[str, Any]:
    """Return a Walmart product payload with display metadata enriched."""
    out = dict(product or {})
    if not is_walmart_product(out):
        return out
    sku = sku_from_product(out)
    if not sku:
        return out

    out.setdefault("asin", sku)
    out["network"] = "walmart"
    out["retailer"] = "Walmart"
    out["retailer_name"] = "Walmart"

    cached = _normalize_cached(_cached_walmart_product(sku))
    out = _merge_metadata(out, cached)

    if fetch_live:
        try:
            if walmart_api is None:
                from product_api import WalmartAPI
                walmart_api = WalmartAPI()
            live = walmart_api.get_item_by_id(sku)
            out = _merge_metadata(out, _normalize_live(live or {}))
        except Exception as exc:
            logging.warning("[WALMART_STOREFRONT] live enrichment failed for %s: %s", sku, exc)

    return out


def enrich_product_list(
    products: list[dict[str, Any]],
    *,
    fetch_live: bool = True,
    walmart_api: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    enriched = []
    stats = {"total": 0, "walmart": 0, "changed": 0}
    for product in products or []:
        if not isinstance(product, dict):
            continue
        stats["total"] += 1
        before = json.dumps(product, sort_keys=True, default=str)
        after = enrich_product_payload(product, fetch_live=fetch_live, walmart_api=walmart_api)
        if is_walmart_product(after):
            stats["walmart"] += 1
        if json.dumps(after, sort_keys=True, default=str) != before:
            stats["changed"] += 1
        enriched.append(after)
    return enriched, stats


def post_update_fields(
    post: dict[str, Any],
    *,
    fetch_live: bool = True,
    walmart_api: Any | None = None,
) -> dict[str, Any]:
    """Return posts-table field updates for a Walmart post row."""
    payload = {
        "asin": post.get("asin"),
        "network": post.get("network") or "walmart",
        "retailer": "Walmart",
        "product_name": post.get("product_name") or "",
        "brand": post.get("product_brand") or "",
        "price": post.get("product_price") or "",
        "image_encoded_string": post.get("product_image") or "",
    }
    enriched = enrich_product_payload(payload, fetch_live=fetch_live, walmart_api=walmart_api)
    updates: dict[str, Any] = {}
    mapping = {
        "product_name": enriched.get("product_name"),
        "product_brand": enriched.get("brand") or enriched.get("company_name"),
        "product_price": enriched.get("price_display") or enriched.get("price"),
        "product_image": enriched.get("image_encoded_string"),
        "product_availability": enriched.get("availability"),
        "product_rating": enriched.get("rating"),
        "product_review_count": enriched.get("review_count"),
    }
    for key, value in mapping.items():
        if value not in (None, "") and str(value) != str(post.get(key) or ""):
            updates[key] = value
    return updates
