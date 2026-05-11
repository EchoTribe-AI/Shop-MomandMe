"""Shared persistence rules for public collection landing pages."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable

import db_schema


VALID_STATUSES = {"draft", "published", "archived"}
DEFAULT_CREATOR_ID = "everydaywithsteph"
DEFAULT_SHOP_SUBDOMAIN = "shop.echotribe.ai"


class CollectionServiceError(RuntimeError):
    """Raised when a collection cannot be safely saved or published."""


def normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:90].strip("-")


def normalize_status(value: str | None, default: str = "published") -> str:
    status = (value or default).strip().lower()
    if status not in VALID_STATUSES:
        raise CollectionServiceError(f"Invalid collection status: {status}")
    return status


def is_walmart_product(product: dict[str, Any]) -> bool:
    fields = (
        str(product.get("network") or ""),
        str(product.get("retailer") or ""),
        str(product.get("retailer_name") or ""),
    )
    return any(field.strip().lower() == "walmart" for field in fields)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_schema.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _loads_list(raw: Any, fallback: list | None = None) -> list:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else (fallback or [])
    except (json.JSONDecodeError, TypeError):
        return fallback or []


def _merge_campaign_types(existing: Any, incoming: list[str] | None) -> list[str]:
    merged = set(_loads_list(existing, []))
    if incoming:
        merged.update(str(item) for item in incoming if item)
    if not merged:
        merged.add("organic")
    return sorted(merged)


def _public_payload(slug: str, status: str, shop_subdomain: str, warnings: list[str], campaign_types: list[str]) -> dict[str, Any]:
    is_draft = status != "published"
    preview_url = f"/shop/{slug}?preview=1"
    public_url = f"https://{shop_subdomain}/{slug}" if not is_draft else preview_url
    return {
        "slug": slug,
        "status": status,
        "is_draft": is_draft,
        "url": preview_url if is_draft else f"/shop/{slug}",
        "preview_url": preview_url,
        "public_url": public_url,
        "campaign_types": campaign_types,
        "warnings": warnings,
    }


def get_collage(slug: str) -> dict[str, Any] | None:
    clean_slug = normalize_slug(slug)
    if not clean_slug:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM collages WHERE slug = ?", (clean_slug,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["products"] = _loads_list(out.pop("products_json", "[]"), [])
        out["campaign_types"] = _loads_list(out.get("campaign_types"), [])
        return out
    finally:
        conn.close()


def list_collages(status: str = "published", limit: int = 50) -> list[dict[str, Any]]:
    status_filter = (status or "published").strip().lower()
    params: list[Any] = []
    where = "COALESCE(status,'published') != 'archived'"
    if status_filter != "all":
        normalize_status(status_filter)
        where = "COALESCE(status,'published') = ?"
        params.append(status_filter)
    params.append(max(1, min(int(limit or 50), 200)))

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT slug, theme, layout, created_at, click_count, products_json, "
            "creator_id, status, campaign_types "
            f"FROM collages WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()

    collages = []
    for row in rows:
        products = _loads_list(row["products_json"], [])
        collages.append({
            "slug": row["slug"],
            "theme": row["theme"],
            "layout": row["layout"],
            "created_at": row["created_at"][:10] if row["created_at"] else "",
            "click_count": row["click_count"] or 0,
            "product_count": len(products),
            "creator_id": row["creator_id"] or DEFAULT_CREATOR_ID,
            "status": row["status"] or "published",
            "campaign_types": _loads_list(row["campaign_types"], ["organic"]),
        })
    return collages


def save_collage(
    payload: dict[str, Any],
    *,
    shop_subdomain: str = DEFAULT_SHOP_SUBDOMAIN,
    link_generator: Callable[[str, str], dict[str, Any] | None] | None = None,
    campaign_types: list[str] | None = None,
) -> dict[str, Any]:
    slug = normalize_slug(payload.get("slug") or "")
    products = payload.get("products") or []
    if not slug or not products:
        raise CollectionServiceError("slug and products required")
    if not isinstance(products, list):
        raise CollectionServiceError("products must be a list")

    status = normalize_status(payload.get("status"), "published")
    creator_id = (payload.get("creator_id") or DEFAULT_CREATOR_ID).strip() or DEFAULT_CREATOR_ID
    warnings: list[str] = []

    for product in products:
        if not isinstance(product, dict):
            raise CollectionServiceError("each product must be an object")
        asin = str(product.get("asin") or "").strip()
        if not asin:
            continue
        if is_walmart_product(product):
            # Explicit architecture rule: Walmart links are owned upstream by the
            # Walmart trend flow. Preserve them exactly and never ask Archer.
            if not product.get("attribution_link"):
                message = f"Walmart product {asin} is missing attribution_link"
                if status == "published":
                    raise CollectionServiceError(message)
                warnings.append(message)
            continue
        if status == "published" and not product.get("attribution_link") and link_generator:
            link = link_generator(asin, f"{slug}-{asin.lower()}")
            if link:
                product["attribution_link"] = link.get("attribution_link") or link.get("url") or ""
            if not product.get("attribution_link"):
                warnings.append(f"Archer attribution link missing for {asin}")

    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT campaign_types FROM collages WHERE slug = ?", (slug,)
        ).fetchone()
        merged_types = _merge_campaign_types(
            existing["campaign_types"] if existing else None,
            campaign_types or ["organic"],
        )
        if existing:
            conn.execute(
                """
                UPDATE collages
                SET products_json = ?, layout = ?, theme = ?, caption = ?,
                    direct_to_amazon = ?, creator_id = ?, status = ?,
                    campaign_types = ?, hero_title = ?, hero_subtitle = ?
                WHERE slug = ?
                """,
                (
                    json.dumps(products),
                    payload.get("layout", "layout-2"),
                    payload.get("theme", "coral"),
                    payload.get("caption", ""),
                    1 if payload.get("direct_to_amazon") else 0,
                    creator_id,
                    status,
                    json.dumps(merged_types),
                    payload.get("hero_title", ""),
                    payload.get("hero_subtitle", ""),
                    slug,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO collages
                (slug, products_json, layout, theme, caption, direct_to_amazon,
                 creator_id, status, campaign_types, hero_title, hero_subtitle)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    json.dumps(products),
                    payload.get("layout", "layout-2"),
                    payload.get("theme", "coral"),
                    payload.get("caption", ""),
                    1 if payload.get("direct_to_amazon") else 0,
                    creator_id,
                    status,
                    json.dumps(merged_types),
                    payload.get("hero_title", ""),
                    payload.get("hero_subtitle", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = _public_payload(slug, status, shop_subdomain, warnings, merged_types)
    result["creator_id"] = creator_id
    return result


def publish_collage(
    slug: str,
    *,
    shop_subdomain: str = DEFAULT_SHOP_SUBDOMAIN,
    link_generator: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    collage = get_collage(slug)
    if not collage:
        raise CollectionServiceError("collection not found")
    payload = {
        **collage,
        "slug": collage["slug"],
        "products": collage["products"],
        "status": "published",
        "direct_to_amazon": bool(collage.get("direct_to_amazon")),
    }
    return save_collage(
        payload,
        shop_subdomain=shop_subdomain,
        link_generator=link_generator,
        campaign_types=collage.get("campaign_types") or ["organic"],
    )
