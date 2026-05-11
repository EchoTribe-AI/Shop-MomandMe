from __future__ import annotations

import csv
import logging
import os
from functools import lru_cache
from typing import Any

from utils.asin import extract_asin


CATALOG_PATH = os.environ.get("ARCHER_FULL_CATALOG_PATH", "data/Archer Full Catalog 2026.csv")
AMAZON_TAG = os.environ.get("AMAZON_ASSOC_TAG", "mommymedeals-20")


def amazon_image_url(asin: str) -> str:
    return (
        "https://ws-na.amazon-adsystem.com/widgets/q?"
        f"_encoding=UTF8&ASIN={asin}&Format=_SL250_&ID=AsinImage"
        "&MarketPlace=US&ServiceVersion=20070822&WS=1"
    )


def amazon_affiliate_url(asin: str, tag: str | None = None) -> str:
    return f"https://www.amazon.com/dp/{asin}?tag={tag or AMAZON_TAG}"


def _clean_price(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.startswith("$") else f"${text}"


def _normalize_product(raw: dict[str, Any], asin: str, source: str) -> dict[str, Any]:
    product = {
        "asin": (raw.get("asin") or raw.get("ASIN") or asin).strip(),
        "product_name": raw.get("product_name") or raw.get("name") or raw.get("Product Titile ") or raw.get("Product Title") or "",
        "company_name": raw.get("company_name") or raw.get("brand") or raw.get("Brand") or "",
        "brand": raw.get("brand") or raw.get("company_name") or raw.get("Brand") or "",
        "price": _clean_price(raw.get("price") or raw.get("Product Price") or ""),
        "commission_payout": raw.get("commission_payout") or raw.get("commission_payout_aff") or raw.get("Affiliate Commission Payout") or "",
        "image_encoded_string": raw.get("image_encoded_string") or raw.get("imageUrl") or raw.get("image") or "",
        "product_category": raw.get("product_category") or raw.get("Category") or "",
        "avg_rating": raw.get("avg_rating") or raw.get("Average Rating") or "",
        "total_reviews": raw.get("total_reviews") or raw.get("Total Reviews") or "",
        "network": raw.get("network") or "amazon",
        "source": source,
    }
    if raw.get("live_price") is not None:
        product["live_price"] = raw["live_price"]
    return product


def _merge_product(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in incoming.items():
        if key == "source":
            if value and value not in (out.get("source") or ""):
                out["source"] = ",".join(filter(None, [out.get("source"), value]))
        elif value not in (None, "") and not out.get(key):
            out[key] = value
    return out


@lru_cache(maxsize=1)
def _catalog_by_asin() -> dict[str, dict[str, Any]]:
    if not os.path.exists(CATALOG_PATH):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    try:
        with open(CATALOG_PATH, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                asin = (row.get("ASIN") or row.get("asin") or "").strip().upper()
                if asin:
                    rows[asin] = row
    except Exception as exc:
        logging.warning("[PRODUCT_LOOKUP] Catalog fallback failed: %s", exc)
    return rows


def _persist_product(archer: Any, product: dict[str, Any]) -> None:
    if not product.get("asin") or not product.get("product_name"):
        return
    try:
        conn = archer._db_connect()
        conn.execute(
            """
            INSERT INTO products
            (asin, company_name, product_name, price, commission_payout,
             product_category, avg_rating, total_reviews, image_encoded_string,
             product_status, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            ON CONFLICT(asin) DO UPDATE SET
                company_name = COALESCE(NULLIF(products.company_name, ''), excluded.company_name),
                product_name = COALESCE(NULLIF(products.product_name, ''), excluded.product_name),
                price = COALESCE(NULLIF(products.price, ''), excluded.price),
                commission_payout = COALESCE(NULLIF(products.commission_payout, ''), excluded.commission_payout),
                product_category = COALESCE(NULLIF(products.product_category, ''), excluded.product_category),
                avg_rating = COALESCE(NULLIF(products.avg_rating, ''), excluded.avg_rating),
                total_reviews = COALESCE(NULLIF(products.total_reviews, ''), excluded.total_reviews),
                image_encoded_string = COALESCE(NULLIF(products.image_encoded_string, ''), excluded.image_encoded_string),
                product_status = COALESCE(NULLIF(products.product_status, ''), 'active')
            """,
            (
                product.get("asin"),
                product.get("company_name") or product.get("brand") or "",
                product.get("product_name") or "",
                product.get("price") or "",
                product.get("commission_payout") or "",
                product.get("product_category") or "",
                product.get("avg_rating") or "",
                product.get("total_reviews") or "",
                product.get("image_encoded_string") or "",
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("[PRODUCT_LOOKUP] Cache persist failed for %s: %s", product.get("asin"), exc)


def resolve_amazon_product(asin_or_url: str, archer: Any | None = None, persist: bool = True) -> dict[str, Any] | None:
    """Resolve one Amazon product through the dashboard's canonical fallback chain."""
    asin = extract_asin(asin_or_url) or (asin_or_url or "").strip().upper()
    if not asin:
        return None

    if archer is None:
        from product_api import ArcherAPI
        archer = ArcherAPI()

    product: dict[str, Any] = {"asin": asin, "network": "amazon", "source": ""}

    try:
        rows = archer.get_by_asins([asin])
        if rows:
            product = _merge_product(product, _normalize_product(rows[0], asin, "cache"))
    except Exception as exc:
        logging.warning("[PRODUCT_LOOKUP] DB lookup failed for %s: %s", asin, exc)

    if not product.get("product_name") or not product.get("image_encoded_string"):
        try:
            live = archer.get_product(asin) or {}
            if live:
                product = _merge_product(product, _normalize_product(live, asin, "archer"))
        except Exception as exc:
            logging.warning("[PRODUCT_LOOKUP] Archer lookup failed for %s: %s", asin, exc)

    catalog_row = _catalog_by_asin().get(asin)
    if catalog_row:
        product = _merge_product(product, _normalize_product(catalog_row, asin, "catalog"))

    if not product.get("product_name") or not product.get("price"):
        try:
            from utils.crawlbase import get_amazon_product
            scraped = get_amazon_product(asin)
            if scraped:
                product = _merge_product(product, _normalize_product(scraped, asin, "crawlbase"))
        except Exception as exc:
            logging.warning("[PRODUCT_LOOKUP] Crawlbase lookup failed for %s: %s", asin, exc)

    if not product.get("image_encoded_string"):
        product["image_encoded_string"] = amazon_image_url(asin)
        product["source"] = ",".join(filter(None, [product.get("source"), "amazon-widget"]))

    if not product.get("product_name"):
        product["product_name"] = asin

    product["affiliate_url"] = amazon_affiliate_url(asin)
    if persist:
        _persist_product(archer, product)
    return product
