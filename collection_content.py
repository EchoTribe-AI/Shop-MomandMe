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
SOURCE_AMAZON_TREND = "amazon_trend"
DEFAULT_CREATOR_ID = "everydaywithsteph"
DEFAULT_PLATFORM = "facebook_group"
DEFAULT_TONE = "warm mom-to-mom"
DEFAULT_CTA = "Shop these finds"
HOOK_FRAMEWORKS = ["Fast Discovery", "Problem → Solution", "Creator Favorites"]


def _collection_retailer(collection: dict[str, Any]) -> str:
    """Return 'walmart', 'amazon', or '' based on collection or its items."""
    direct = str(collection.get("retailer") or "").strip().lower()
    if direct in ("walmart", "amazon"):
        return direct
    seen: set[str] = set()
    for item in collection.get("items") or []:
        if not isinstance(item, dict):
            continue
        for field in ("retailer", "network", "retailer_name"):
            value = str(item.get(field) or "").strip().lower()
            if value in ("walmart", "amazon"):
                seen.add(value)
                break
    if len(seen) == 1:
        return next(iter(seen))
    return ""


def _retailer_label(retailer: str) -> str:
    if retailer == "walmart":
        return "Walmart"
    if retailer == "amazon":
        return "Amazon"
    return ""


def default_cta_for_retailer(retailer: str) -> str:
    label = _retailer_label(retailer)
    if label:
        return f"Shop the {label} finds"
    return "Shop these finds"


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
    try:
        data = get_trending_page_data()
    except Exception:
        return None
    for collection in data.get("collections", []):
        if collection.get("slug") == slug:
            return collection
    return None


def get_latest_draft_for_public_slug(public_slug: str) -> dict[str, Any] | None:
    clean_slug = slugify(public_slug)
    if not clean_slug:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT id FROM collection_content_drafts
            WHERE source_type = ?
              AND (public_slug = ? OR published_collage_slug = ?)
            ORDER BY
              CASE WHEN status = 'published' THEN 0 ELSE 1 END,
              updated_at DESC,
              id DESC
            LIMIT 1
            """,
            (SOURCE_WALMART_TREND, clean_slug, clean_slug),
        ).fetchone()
        return get_draft(int(row["id"])) if row else None
    finally:
        conn.close()


def collection_from_draft_snapshot(draft: dict[str, Any]) -> dict[str, Any]:
    """Build an editor-safe collection from a saved product snapshot.

    Carries retailer/network through so the editor can show "Walmart" or
    "Amazon" labels for an edit of an already-published page.
    """
    items = []
    detected_retailers: set[str] = set()
    for idx, product in enumerate(draft.get("product_snapshot") or [], start=1):
        if not isinstance(product, dict):
            continue
        retailer = ""
        for field in ("retailer", "network", "retailer_name"):
            value = str(product.get(field) or "").strip().lower()
            if value in ("walmart", "amazon"):
                retailer = value
                break
        if retailer:
            detected_retailers.add(retailer)
        items.append({
            "sku": str(product.get("asin") or product.get("sku") or ""),
            "title": product.get("product_name") or product.get("title") or "Trend find",
            "brand": product.get("brand") or product.get("company_name") or "",
            "price_display": product.get("price_display") or product.get("price") or "",
            "current_price": product.get("current_price") or "",
            "image_url": product.get("image_encoded_string") or product.get("image_url") or "",
            "shop_url": product.get("attribution_link") or product.get("shop_url") or "",
            "category": product.get("category") or "",
            "rank": product.get("rank") or product.get("source_rank") or idx,
            "badges": product.get("source_badges") or [],
            "retailer": retailer,
            "network": retailer,
        })
    out = {
        "slug": draft.get("source_collection_slug") or "",
        "name": draft.get("title") or "Trend finds",
        "description": draft.get("description") or "",
        "items": items,
    }
    if len(detected_retailers) == 1:
        out["retailer"] = next(iter(detected_retailers))
    return out


def walmart_product_context(collection: dict[str, Any], limit: int = 10) -> list[dict[str, str]]:
    """Normalize first products for AI context while preserving existing shop_url."""
    products = []
    for product in (collection.get("items") or [])[:limit]:
        # Use the actual product retailer (walmart/amazon), not a hardcoded label.
        product_retailer = ""
        for field in ("retailer", "network", "retailer_name"):
            value = str(product.get(field) or "").strip().lower()
            if value in ("walmart", "amazon"):
                product_retailer = "Walmart" if value == "walmart" else "Amazon"
                break
        products.append({
            "sku": str(product.get("sku") or ""),
            "name": str(product.get("title") or "find"),
            "brand": str(product.get("brand") or ""),
            "price": str(product.get("price_display") or ""),
            "retailer": product_retailer or "Walmart",
            "shop_url": str(product.get("shop_url") or ""),
        })
    return products


def _product_retailer_key(product: dict[str, Any]) -> str:
    """Detect retailer from a source product dict: 'walmart' | 'amazon' | ''."""
    for field in ("retailer", "network", "retailer_name"):
        value = str(product.get(field) or "").strip().lower()
        if value in ("walmart", "amazon"):
            return value
    return ""


def adapt_walmart_products_for_collage(collection: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    """Adapt collection products (Walmart or Amazon) to /shop/<slug> product shape.

    Walmart items go through walmart_enrichment for display metadata. Amazon items
    skip the Walmart enrichment (it silently no-ops on non-Walmart anyway, but we
    skip explicitly and label them correctly).

    Raises if any product lacks shop_url so we never fall back to a hardcoded URL.
    """
    adapted = []
    source_items = collection.get("items") or []
    if limit is not None:
        source_items = source_items[:limit]
    for product in source_items:
        sku = str(product.get("sku") or "").strip()
        shop_url = str(product.get("shop_url") or "").strip()
        if not sku or not shop_url:
            raise CollectionContentError(f"Product {sku or '(missing sku)'} is missing shop_url")
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
        retailer_key = _product_retailer_key(product) or "walmart"
        retailer_label_text = "Amazon" if retailer_key == "amazon" else "Walmart"
        default_name = f"{retailer_label_text} find"
        adapted_product = {
            "asin": sku,
            "product_name": product.get("title") or default_name,
            "company_name": product.get("brand") or "",
            "brand": product.get("brand") or "",
            "price": product.get("price_display") or product.get("current_price") or "",
            "current_price": product.get("current_price") or product.get("price_display") or "",
            "price_display": product.get("price_display") or "",
            "image_encoded_string": product.get("image_url") or "",
            "attribution_link": shop_url,
            "retailer": retailer_label_text,
            "retailer_name": retailer_label_text,
            "network": retailer_key,
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
        if retailer_key == "amazon":
            # Amazon products: skip Walmart-only enrichment.
            adapted.append(adapted_product)
        else:
            adapted.append(walmart_enrichment.enrich_product_payload(adapted_product))
    if not adapted:
        raise CollectionContentError("Collection has no products to publish")
    return adapted


def _demo_generation(collection: dict[str, Any], voice_source_text: str) -> dict[str, Any]:
    retailer = _collection_retailer(collection)
    label = _retailer_label(retailer)
    retail_phrase = label if label else ""
    finds_phrase = f"{label} finds" if label else "finds"
    title = collection.get("name") or finds_phrase
    note = voice_source_text.strip() or f"I pulled together a few {finds_phrase} that caught my eye."
    cleaned = note.replace("Steph voice:", "").strip()
    hook_fast = (
        f"I found a quick {retail_phrase} roundup for {title.lower()}"
        if retail_phrase
        else f"I found a quick roundup for {title.lower()}"
    )
    hook_problem = (
        f"If your weekend list is scattered, these {finds_phrase} put the useful stuff in one place"
    )
    hook_fav = (
        f"Steph’s {retail_phrase} picks for the yard, kids, and little home wins"
        if retail_phrase
        else "Steph’s picks for the yard, kids, and little home wins"
    )
    social = (
        f"{cleaned}\n\nI rounded up the {finds_phrase} in one spot so you can skim them fast and "
        "decide what’s worth checking out. [collection link]"
    )
    if retail_phrase:
        landing = (
            f"I pulled together this {title.lower()} page so you can quickly browse the "
            f"{finds_phrase} from the post. Check the product cards below, compare the current "
            f"{retail_phrase} price, and grab whatever fits your home, yard, or family."
        )
    else:
        landing = (
            f"I pulled together this {title.lower()} page so you can quickly browse the "
            f"{finds_phrase} from the post. Check the product cards below, compare current "
            "prices, and grab whatever fits your home, yard, or family."
        )
    return _normalize_generated({
        "cleaned_transcript": cleaned,
        "hooks": [
            {"type": "Fast Discovery", "text": hook_fast},
            {"type": "Problem → Solution", "text": hook_problem},
            {"type": "Creator Favorites", "text": hook_fav},
        ],
        "social_post": social,
        "landing_intro": landing,
        "cta": default_cta_for_retailer(retailer),
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

    retailer_key_value = _collection_retailer(collection)
    retailer_label_text = _retailer_label(retailer_key_value) or "Walmart"
    cta_default = default_cta_for_retailer(retailer_key_value)
    product_lines = "\n".join(
        f"- {p['name']} | brand: {p['brand'] or 'n/a'} | price: {p['price'] or 'price not shown'} | retailer: {p.get('retailer') or retailer_label_text}"
        for p in products
    )
    system = (
        "You create creator-voice social post and landing page copy for shoppable trend collections. "
        "Use the existing Ryze/MCP-style campaign discipline: separate the hook angle from body copy, make every output structured, and keep CTA/link handling explicit. "
        "Return ONLY valid JSON with keys cleaned_transcript, hooks, social_post, landing_intro, cta, link_placeholder. "
        "hooks must be exactly three objects with these exact types in order: Fast Discovery, Problem → Solution, Creator Favorites. "
        "Fast Discovery = quick timely find/roundup; Problem → Solution = practical problem solved by the collection; Creator Favorites = Steph/creator-curated picks. "
        "Rules: preserve the creator voice from pasted/transcribed notes; use the creator voice_prompt if provided; "
        f"use the creator's words as the primary source; use the {retailer_label_text} collection title and products as context; "
        "do not invent product claims, personal ownership, personal experience, prices, availability, urgency, or scarcity; "
        "do not mention earnings, units, workbook, API, Impact, URLGenius, backend, or internal data; "
        "make social_post click-driving in Facebook group style; make landing_intro support the shoppable page and not duplicate the social post; "
        f"mention {retailer_label_text} and the collection theme naturally; use 0-3 emojis max unless the creator voice clearly uses more; "
        "include [collection link] as the link placeholder, never a raw URL."
    )
    user = (
        f"Creator: {creator.get('display_name') or creator_id} {creator.get('handle') or ''}\n"
        f"Creator voice prompt: {creator.get('voice_prompt') or ''}\n"
        f"Pasted creator voice/notes: {voice_source_text}\n\n"
        f"Platform: {platform}\nTone: {tone}\nAudience: {audience_context}\n\n"
        f"Retailer: {retailer_label_text}\n"
        f"Collection title: {collection.get('name') or ''}\n"
        f"Collection description: {collection.get('description') or ''}\n"
        f"Products:\n{product_lines}\n\n"
        "Return JSON exactly like: "
        '{"cleaned_transcript":"cleaned-up creator words","hooks":[{"type":"Fast Discovery","text":"..."},{"type":"Problem → Solution","text":"..."},{"type":"Creator Favorites","text":"..."}],"social_post":"...","landing_intro":"...","cta":"'
        + cta_default
        + '","link_placeholder":"[collection link]"}'
    )
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    # Try the preferred model, then fall back to a known-good alias, then to
    # demo copy. We've seen Anthropic return a transient 500 on certain inputs;
    # never let that fully block the demo flow.
    model_candidates = ["claude-sonnet-4-6", "claude-sonnet-4-5", "claude-3-5-sonnet-latest"]
    last_err: Exception | None = None
    for model_name in model_candidates:
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=1800,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            raw = message.content[0].text if message.content else "{}"
            try:
                return _normalize_generated(_extract_json_object(raw))
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = exc
                continue
        except anthropic.APIError as exc:
            last_err = exc
            continue
        except Exception as exc:
            last_err = exc
            break
    # All AI attempts failed — fall through to demo so the demo flow stays usable.
    generated = _demo_generation(collection, voice_source_text)
    err_text = str(last_err) if last_err else "AI generation unavailable"
    generated["warning"] = (
        f"AI generation hit an error ({err_text[:200]}); showing an editable demo draft."
    )
    return generated


def save_walmart_collection_draft(
    collection_slug: str,
    payload: dict[str, Any],
    status: str = "draft",
) -> dict[str, Any]:
    collection = get_walmart_collection(collection_slug)
    existing_draft = get_draft(int(payload.get("draft_id"))) if payload.get("draft_id") else None
    if collection:
        products = adapt_walmart_products_for_collage(collection)
    elif existing_draft and existing_draft.get("product_snapshot"):
        collection = collection_from_draft_snapshot(existing_draft)
        products = existing_draft.get("product_snapshot") or []
    else:
        raise CollectionContentError("Walmart collection not found")
    creator_id = _clean_text(payload.get("creator_id"), 120) or DEFAULT_CREATOR_ID
    retailer = _collection_retailer(collection)
    title = _clean_text(payload.get("title"), 240) or collection.get("name") or (_retailer_label(retailer) + " finds" if _retailer_label(retailer) else "Trending finds")
    description = _clean_text(payload.get("description"), 500) or collection.get("description") or ""
    slug_prefix = retailer if retailer else "trend"
    public_slug = slugify(payload.get("public_slug") or f"{slug_prefix}-{collection_slug}")
    # Editor design controls (allow-list).
    valid_themes = {"coral", "peach", "sage", "sand", "midnight"}
    valid_layouts = {"layout-2", "layout-3", "layout-4", "layout-featured"}
    theme = _clean_text(payload.get("theme"), 40) or "peach"
    if theme not in valid_themes:
        theme = "peach"
    layout = _clean_text(payload.get("layout"), 40) or "layout-2"
    if layout not in valid_layouts:
        layout = "layout-2"
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
        "cta": _clean_text(payload.get("cta"), 120) or default_cta_for_retailer(retailer),
        "platform": _clean_text(payload.get("platform"), 80) or DEFAULT_PLATFORM,
        "tone": _clean_text(payload.get("tone"), 160),
        "product_snapshot_json": json.dumps(products),
        "status": status,
        "public_slug": public_slug,
        "published_collage_slug": payload.get("published_collage_slug") or "",
        "theme": theme,
        "layout": layout,
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



def _shopper_safe_description(description: Any, retailer: str = "") -> str:
    """Keep published Trend subtitles shopper-facing, retailer-aware."""
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
        label = _retailer_label(retailer)
        if label:
            return f"Fresh {label} finds shoppers are checking out right now."
        return "Fresh finds shoppers are checking out right now."
    return text

def _upsert_collage_from_draft(draft: dict[str, Any], publish: bool) -> dict[str, str]:
    products = draft.get("product_snapshot") or []
    if not products:
        raise CollectionContentError("Draft has no product snapshot")
    public_slug = slugify(draft.get("public_slug") or f"walmart-{draft['source_collection_slug']}")
    status = "published" if publish else "draft"
    creator_id = draft.get("creator_id") or DEFAULT_CREATOR_ID

    # Detect dominant retailer from products to tag campaign_types correctly.
    seen_retailers: set[str] = set()
    for product in products:
        if not isinstance(product, dict):
            continue
        for field in ("retailer", "network", "retailer_name"):
            value = str(product.get(field) or "").strip().lower()
            if value in ("walmart", "amazon"):
                seen_retailers.add(value)
                break
    if seen_retailers == {"amazon"}:
        campaign_types = [SOURCE_AMAZON_TREND]
        retailer_for_subtitle = "amazon"
    elif seen_retailers == {"walmart"}:
        campaign_types = [SOURCE_WALMART_TREND]
        retailer_for_subtitle = "walmart"
    elif seen_retailers:
        campaign_types = sorted(
            {SOURCE_AMAZON_TREND if r == "amazon" else SOURCE_WALMART_TREND for r in seen_retailers}
        )
        retailer_for_subtitle = ""
    else:
        campaign_types = [SOURCE_WALMART_TREND]
        retailer_for_subtitle = ""

    draft_theme = (draft.get("theme") or "").strip() or "peach"
    draft_layout = (draft.get("layout") or "").strip() or ("layout-3" if len(products) >= 6 else "layout-2")
    try:
        result = collection_service.save_collage(
            {
                "slug": public_slug,
                "products": products,
                "layout": draft_layout,
                "theme": draft_theme,
                "caption": draft.get("landing_intro") or "",
                "direct_to_amazon": False,
                "creator_id": creator_id,
                "status": status,
                "hero_title": draft.get("title") or public_slug.replace("-", " ").title(),
                "hero_subtitle": _shopper_safe_description(draft.get("description"), retailer_for_subtitle),
            },
            shop_subdomain=os.environ.get("SHOP_SUBDOMAIN", "shop.echotribe.ai").lower(),
            campaign_types=campaign_types,
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
