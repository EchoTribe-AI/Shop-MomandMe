"""Creator-voice content drafts for trend collections.

This module intentionally reads existing Walmart Trending `shop_url` values and
copies them into draft/public page product payloads. It does not call Impact,
URLGenius, or any product link generation code.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any

import anthropic

import collection_service
import db_schema
import walmart_storefront_enrichment as walmart_enrichment

SOURCE_WALMART_TREND = "walmart_trend"
DEFAULT_CREATOR_ID = "everydaywithsteph"
DEFAULT_PLATFORM = "facebook_group"
DEFAULT_TONE = "warm mom-to-mom"
DEFAULT_CTA = "Shop the Walmart finds"
HOOK_FRAMEWORKS = ["Fast Discovery", "Problem → Solution", "Creator Favorites"]


class CollectionContentError(RuntimeError):
    """Raised for validation or persistence failures in collection content flow."""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_schema.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")


def slugify(value: str, fallback: str = "walmart-trend-page") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:90].strip("-") or fallback


def _clean_text(value: Any, limit: int = 20000) -> str:
    return str(value or "").strip()[:limit]


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("AI response must be a JSON object")
    return parsed


def _normalize_hook(raw: Any, idx: int) -> dict[str, str]:
    hook_type = HOOK_FRAMEWORKS[idx] if idx < len(HOOK_FRAMEWORKS) else f"Hook {idx + 1}"
    if isinstance(raw, dict):
        hook_type = _clean_text(raw.get("type"), 80) or hook_type
        text = _clean_text(raw.get("text"), 240)
    else:
        text = _clean_text(raw, 240)
    return {"type": hook_type, "text": text}


def _normalize_generated(payload: dict[str, Any]) -> dict[str, Any]:
    hooks_raw = payload.get("hooks") or []
    if not isinstance(hooks_raw, list):
        hooks_raw = []
    hooks = []
    for idx in range(3):
        raw = hooks_raw[idx] if idx < len(hooks_raw) else {}
        hook = _normalize_hook(raw, idx)
        if not hook["text"]:
            hook["text"] = [
                "Fresh Walmart finds worth checking out right now",
                "A few easy finds that solve the weekend errand scramble",
                "Steph’s quick Walmart picks to skim before your next run",
            ][idx]
        hooks.append(hook)
    cleaned = _clean_text(
        payload.get("cleaned_transcript")
        or payload.get("cleaned_voice")
        or payload.get("voice_source_text"),
        10000,
    )
    return {
        "cleaned_transcript": cleaned,
        "social_post": _clean_text(payload.get("social_post"), 5000),
        "landing_intro": _clean_text(payload.get("landing_intro"), 3000),
        "hooks": hooks,
        "cta": _clean_text(payload.get("cta"), 120) or DEFAULT_CTA,
        "link_placeholder": _clean_text(payload.get("link_placeholder"), 80) or "[collection link]",
    }

def get_walmart_collection(collection_slug: str) -> dict[str, Any] | None:
    """Return one active Walmart Trending collection from the existing page data."""
    from walmart_trends import get_trending_page_data

    slug = (collection_slug or "").strip()
    data = get_trending_page_data()
    for collection in data.get("collections", []):
        if collection.get("slug") == slug:
            return collection
    return None


def walmart_product_context(collection: dict[str, Any], limit: int = 10) -> list[dict[str, str]]:
    """Normalize first products for AI context while preserving existing shop_url."""
    products = []
    for product in (collection.get("items") or [])[:limit]:
        products.append({
            "sku": str(product.get("sku") or ""),
            "name": str(product.get("title") or "Walmart find"),
            "brand": str(product.get("brand") or ""),
            "price": str(product.get("price_display") or ""),
            "retailer": "Walmart",
            "shop_url": str(product.get("shop_url") or ""),
        })
    return products


def adapt_walmart_products_for_collage(collection: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    """Adapt Walmart products to existing /shop/<slug> product shape.

    Raises if any product lacks the existing shop_url, so Walmart pages never fall
    back to the Amazon URL in the shared landing page template.
    """
    adapted = []
    source_items = collection.get("items") or []
    if limit is not None:
        source_items = source_items[:limit]
    for product in source_items:
        sku = str(product.get("sku") or "").strip()
        shop_url = str(product.get("shop_url") or "").strip()
        if not sku or not shop_url:
            raise CollectionContentError(f"Walmart product {sku or '(missing sku)'} is missing shop_url")
        metadata = product.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        source_rank = (
            product.get("source_rank")
            or product.get("rank")
            or metadata.get("rank")
            or (metadata.get("1A") or {}).get("rank")
            or (metadata.get("1B") or {}).get("rank")
        )
        adapted_product = {
            "asin": sku,
            "product_name": product.get("title") or "Walmart find",
            "company_name": product.get("brand") or "",
            "brand": product.get("brand") or "",
            "price": product.get("price_display") or product.get("current_price") or "",
            "current_price": product.get("current_price") or product.get("price_display") or "",
            "price_display": product.get("price_display") or "",
            "image_encoded_string": product.get("image_url") or "",
            "attribution_link": shop_url,
            "retailer": "Walmart",
            "retailer_name": "Walmart",
            "network": "walmart",
            "category": product.get("category") or "",
            "item_count": product.get("item_count"),
            "source_rank": source_rank,
            "rank": source_rank,
            "source_badges": product.get("badges") or [],
        }
        for optional_field in (
            "sale_amount",
            "total_earnings",
            "retail_price",
            "list_price",
            "was_price",
            "original_price",
            "strike_price",
            "msrp",
            "compare_at_price",
        ):
            if product.get(optional_field) not in (None, ""):
                adapted_product[optional_field] = product.get(optional_field)
        adapted.append(walmart_enrichment.enrich_product_payload(adapted_product))
    if not adapted:
        raise CollectionContentError("Collection has no products to publish")
    return adapted


def _demo_generation(collection: dict[str, Any], voice_source_text: str) -> dict[str, Any]:
    title = collection.get("name") or "Walmart finds"
    note = voice_source_text.strip() or "I pulled together a few Walmart finds that caught my eye."
    cleaned = note.replace("Steph voice:", "").strip()
    return _normalize_generated({
        "cleaned_transcript": cleaned,
        "hooks": [
            {"type": "Fast Discovery", "text": f"I found a quick Walmart roundup for {title.lower()}"},
            {"type": "Problem → Solution", "text": "If your weekend list is scattered, these Walmart finds put the useful stuff in one place"},
            {"type": "Creator Favorites", "text": "Steph’s Walmart picks for the yard, kids, and little home wins"},
        ],
        "social_post": f"{cleaned}\n\nI rounded up the Walmart finds in one spot so you can skim them fast and decide what’s worth checking out. [collection link]",
        "landing_intro": f"I pulled together this {title.lower()} page so you can quickly browse the Walmart finds from the post. Check the product cards below, compare the current Walmart price, and grab whatever fits your home, yard, or family.",
        "cta": DEFAULT_CTA,
        "link_placeholder": "[collection link]",
    })

def generate_walmart_collection_content(
    collection_slug: str,
    creator_id: str = DEFAULT_CREATOR_ID,
    voice_source_text: str = "",
    platform: str = DEFAULT_PLATFORM,
    tone: str = DEFAULT_TONE,
    audience_context: str = "busy moms looking for timely Walmart finds",
    allow_demo_fallback: bool = False,
    regenerate_target: str = "",
) -> dict[str, Any]:
    """Generate strict JSON content for a Walmart trend collection."""
    collection = get_walmart_collection(collection_slug)
    if not collection:
        raise CollectionContentError("Walmart collection not found")
    products = walmart_product_context(collection)
    if not products:
        raise CollectionContentError("Walmart collection has no products")

    creator = db_schema.get_creator(creator_id or DEFAULT_CREATOR_ID)
    voice_source_text = _clean_text(voice_source_text, 10000)
    platform = _clean_text(platform, 80) or DEFAULT_PLATFORM
    tone = _clean_text(tone, 160) or DEFAULT_TONE
    audience_context = _clean_text(audience_context, 500)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        if allow_demo_fallback:
            generated = _demo_generation(collection, voice_source_text)
            generated["warning"] = "ANTHROPIC_API_KEY not configured; visible demo fallback generated editable draft copy."
            return generated
        raise CollectionContentError("AI key missing: ANTHROPIC_API_KEY is not configured. Enable Demo fallback or add the key to generate with Claude.")

    product_lines = "\n".join(
        f"- {p['name']} | brand: {p['brand'] or 'n/a'} | price: {p['price'] or 'price not shown'} | retailer: Walmart"
        for p in products
    )
    system = (
        "You create creator-voice social post and landing page copy for shoppable trend collections. "
        "Use the existing Ryze/MCP-style campaign discipline: separate the hook angle from body copy, make every output structured, and keep CTA/link handling explicit. "
        "Return ONLY valid JSON with keys cleaned_transcript, hooks, social_post, landing_intro, cta, link_placeholder. "
        "hooks must be exactly three objects with these exact types in order: Fast Discovery, Problem → Solution, Creator Favorites. "
        "Fast Discovery = quick timely find/roundup; Problem → Solution = practical problem solved by the collection; Creator Favorites = Steph/creator-curated picks. "
        "Rules: preserve the creator voice from pasted/transcribed notes; use the creator voice_prompt if provided; "
        "use the creator's words as the primary source; use Walmart collection title and products as context; "
        "do not invent product claims, personal ownership, personal experience, prices, availability, urgency, or scarcity; "
        "do not mention earnings, units, workbook, API, Impact, URLGenius, backend, or internal data; "
        "make social_post click-driving in Facebook group style; make landing_intro support the shoppable page and not duplicate the social post; "
        "mention Walmart and the collection theme naturally; use 0-3 emojis max unless the creator voice clearly uses more; "
        "include [collection link] as the link placeholder, never a raw URL."
    )
    user = (
        f"Creator: {creator.get('display_name') or creator_id} {creator.get('handle') or ''}\n"
        f"Creator voice prompt: {creator.get('voice_prompt') or ''}\n"
        f"Pasted creator voice/notes: {voice_source_text}\n\n"
        f"Platform: {platform}\nTone: {tone}\nAudience: {audience_context}\n\n"
        f"Collection title: {collection.get('name') or ''}\n"
        f"Collection description: {collection.get('description') or ''}\n"
        f"Products:\n{product_lines}\n\n"
        "Return JSON exactly like: "
        '{"cleaned_transcript":"cleaned-up creator words","hooks":[{"type":"Fast Discovery","text":"..."},{"type":"Problem → Solution","text":"..."},{"type":"Creator Favorites","text":"..."}],"social_post":"...","landing_intro":"...","cta":"Shop the Walmart finds","link_placeholder":"[collection link]"}'
    )
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1800,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = message.content[0].text if message.content else "{}"
    return _normalize_generated(_extract_json_object(raw))


def save_walmart_collection_draft(
    collection_slug: str,
    payload: dict[str, Any],
    status: str = "draft",
) -> dict[str, Any]:
    collection = get_walmart_collection(collection_slug)
    if not collection:
        raise CollectionContentError("Walmart collection not found")
    products = adapt_walmart_products_for_collage(collection)
    creator_id = _clean_text(payload.get("creator_id"), 120) or DEFAULT_CREATOR_ID
    title = _clean_text(payload.get("title"), 240) or collection.get("name") or "Walmart finds"
    description = _clean_text(payload.get("description"), 500) or collection.get("description") or ""
    public_slug = slugify(payload.get("public_slug") or f"walmart-{collection_slug}")
    hooks_raw = payload.get("hooks") or []
    if not isinstance(hooks_raw, list):
        hooks_raw = []
    hooks = []
    for idx in range(3):
        raw = hooks_raw[idx] if idx < len(hooks_raw) else {}
        hook = _normalize_hook(raw, idx)
        if hook["text"]:
            hooks.append(hook)
    now = _now()
    draft_id = payload.get("draft_id")
    fields = {
        "source_type": SOURCE_WALMART_TREND,
        "source_collection_slug": collection_slug,
        "source_collection_id": collection_slug,
        "creator_id": creator_id,
        "title": title,
        "description": description,
        "voice_source_text": _clean_text(payload.get("voice_source_text"), 10000),
        "voice_raw_transcript": _clean_text(payload.get("voice_raw_transcript"), 10000),
        "cleaned_transcript": _clean_text(payload.get("cleaned_transcript"), 10000),
        "social_post": _clean_text(payload.get("social_post"), 5000),
        "landing_intro": _clean_text(payload.get("landing_intro"), 3000),
        "hooks_json": json.dumps(hooks),
        "cta": _clean_text(payload.get("cta"), 120) or DEFAULT_CTA,
        "platform": _clean_text(payload.get("platform"), 80) or DEFAULT_PLATFORM,
        "tone": _clean_text(payload.get("tone"), 160),
        "product_snapshot_json": json.dumps(products),
        "status": status,
        "public_slug": public_slug,
        "published_collage_slug": payload.get("published_collage_slug") or "",
        "updated_at": now,
    }
    conn = _connect()
    try:
        if draft_id:
            row = conn.execute("SELECT id FROM collection_content_drafts WHERE id = ?", (draft_id,)).fetchone()
        else:
            row = None
        if row:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(
                f"UPDATE collection_content_drafts SET {assignments} WHERE id = ?",
                [*fields.values(), draft_id],
            )
            saved_id = int(draft_id)
        else:
            fields["created_at"] = now
            columns = ", ".join(fields.keys())
            placeholders = ", ".join("?" for _ in fields)
            cur = conn.execute(
                f"INSERT INTO collection_content_drafts ({columns}) VALUES ({placeholders})",
                list(fields.values()),
            )
            saved_id = int(cur.lastrowid)
        conn.commit()
        return get_draft(saved_id) or {}
    finally:
        conn.close()


def get_draft(draft_id: int) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM collection_content_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            return None
        draft = dict(row)
        try:
            draft["hooks"] = json.loads(draft.get("hooks_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            draft["hooks"] = []
        try:
            draft["product_snapshot"] = json.loads(draft.get("product_snapshot_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            draft["product_snapshot"] = []
        return draft
    finally:
        conn.close()



def _shopper_safe_description(description: Any) -> str:
    """Keep published Walmart Trend subtitles shopper-facing."""
    text = _clean_text(description, 300)
    lowered = text.lower()
    internal_markers = (
        "units",
        "earnings",
        "workbook",
        "backend",
        "api",
        "item count",
        "curated walmart picks across",
    )
    if not text or any(marker in lowered for marker in internal_markers):
        return "Fresh Walmart finds shoppers are checking out right now."
    return text

def _upsert_collage_from_draft(draft: dict[str, Any], publish: bool) -> dict[str, str]:
    products = draft.get("product_snapshot") or []
    if not products:
        raise CollectionContentError("Draft has no product snapshot")
    public_slug = slugify(draft.get("public_slug") or f"walmart-{draft['source_collection_slug']}")
    status = "published" if publish else "draft"
    creator_id = draft.get("creator_id") or DEFAULT_CREATOR_ID

    try:
        result = collection_service.save_collage(
            {
                "slug": public_slug,
                "products": products,
                "layout": "layout-2" if len(products) < 6 else "layout-3",
                "theme": "peach",
                "caption": draft.get("landing_intro") or "",
                "direct_to_amazon": False,
                "creator_id": creator_id,
                "status": status,
                "hero_title": draft.get("title") or public_slug.replace("-", " ").title(),
                "hero_subtitle": _shopper_safe_description(draft.get("description")),
            },
            shop_subdomain=os.environ.get("SHOP_SUBDOMAIN", "shop.echotribe.ai").lower(),
            campaign_types=[SOURCE_WALMART_TREND],
        )
    except collection_service.CollectionServiceError as exc:
        raise CollectionContentError(str(exc)) from exc

    conn = _connect()
    try:
        now = _now()
        conn.execute(
            """
            UPDATE collection_content_drafts
            SET status = ?, public_slug = ?, published_collage_slug = ?,
                published_at = CASE WHEN ? THEN ? ELSE published_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (status, public_slug, public_slug, 1 if publish else 0, now, now, draft["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "public_slug": public_slug,
        "public_url": result["public_url"],
        "preview_url": result["preview_url"],
        "insights_url": f"/insights?creator_id={creator_id}",
        "status": status,
        "warnings": result.get("warnings", []),
    }


def materialize_preview(draft_id: int) -> dict[str, str]:
    draft = get_draft(draft_id)
    if not draft:
        raise CollectionContentError("Draft not found")
    return _upsert_collage_from_draft(draft, publish=False)


def publish_draft(draft_id: int) -> dict[str, str]:
    draft = get_draft(draft_id)
    if not draft:
        raise CollectionContentError("Draft not found")
    return _upsert_collage_from_draft(draft, publish=True)
