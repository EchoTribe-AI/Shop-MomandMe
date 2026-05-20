"""Canonical retrieval and lightweight memory for public storefront chat."""
from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any, Callable

import db_schema


DEFAULT_CREATOR_ID = "everydaywithsteph"
MAX_TURNS = 8
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "for",
    "from", "give", "have", "i", "in", "is", "it", "me", "my", "of", "on",
    "or", "our", "show", "that", "the", "these", "this", "to", "under",
    "want", "what", "with", "you",
}


def _connect():
    return db_schema._connect()


def _table_exists(conn, table: str) -> bool:
    if db_schema._USE_PG:
        row = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
    return row is not None


def _loads_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _tokens(value: Any) -> list[str]:
    return [
        token for token in re.split(r"[^a-z0-9]+", _clean_text(value).lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


def _format_display_price(raw: Any) -> str:
    text = _clean_text(raw)
    if not text:
        return ""
    if "$" in text:
        return text
    if re.fullmatch(r"\d+(\.\d{1,2})?", text):
        return f"${text}"
    match = re.fullmatch(r"(\d+(?:\.\d{1,2})?)\s*[-]\s*(\d+(?:\.\d{1,2})?)", text)
    if match:
        return f"${match.group(1)}-${match.group(2)}"
    return text


def _network_from_product(product: dict[str, Any], fallback: str = "amazon") -> str:
    fields = (
        product.get("network"),
        product.get("retailer"),
        product.get("retailer_name"),
    )
    for field in fields:
        text = _clean_text(field).lower()
        if text == "walmart":
            return "walmart"
        if text in {"amazon", "archer", "levanta"}:
            return "amazon"
    return fallback or "amazon"


def _product_id(product: dict[str, Any]) -> str:
    return _clean_text(
        product.get("asin")
        or product.get("sku")
        or product.get("item_id")
        or product.get("id")
    )


def _source_search_text(product: dict[str, Any], *extras: Any) -> str:
    return " ".join(
        filter(
            None,
            [
                _clean_text(product.get("product_name") or product.get("name") or product.get("title")),
                _clean_text(product.get("company_name") or product.get("brand")),
                _clean_text(product.get("product_category") or product.get("category")),
                _clean_text(product.get("retailer") or product.get("network")),
                *(_clean_text(extra) for extra in extras),
            ],
        )
    )


def _candidate_base(product: dict[str, Any], source: str, *search_extras: Any) -> dict[str, Any] | None:
    item_id = _product_id(product)
    if not item_id:
        return None
    network = _network_from_product(product)
    link = _clean_text(
        product.get("attribution_link")
        or product.get("smart_link")
        or product.get("shop_url")
        or product.get("link")
        or product.get("url")
    )
    return {
        "id": item_id,
        "asin": item_id,
        "network": network,
        "retailer": "Walmart" if network == "walmart" else "Amazon",
        "name": _clean_text(product.get("product_name") or product.get("name") or product.get("title")) or item_id,
        "brand": _clean_text(product.get("company_name") or product.get("brand")),
        "category": _clean_text(product.get("product_category") or product.get("category")),
        "price": _format_display_price(
            product.get("price_display")
            or product.get("current_price")
            or product.get("price")
            or product.get("product_price")
        ),
        "image": _clean_text(product.get("image_encoded_string") or product.get("product_image") or product.get("image_url") or product.get("image")),
        "availability": _clean_text(product.get("availability") or product.get("product_availability")),
        "rating": product.get("rating") or product.get("product_rating"),
        "review_count": product.get("review_count") or product.get("product_review_count"),
        "link": link,
        "sources": {source},
        "collection_slugs": set(),
        "post_slugs": set(),
        "clicks": 0,
        "popularity": 0.0,
        "search_text": _source_search_text(product, *search_extras),
    }


def _merge_candidate(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    for key in ("name", "brand", "category", "price", "image", "link"):
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]
    existing["sources"].update(incoming.get("sources", set()))
    existing["collection_slugs"].update(incoming.get("collection_slugs", set()))
    existing["post_slugs"].update(incoming.get("post_slugs", set()))
    existing["clicks"] = max(int(existing.get("clicks") or 0), int(incoming.get("clicks") or 0))
    existing["popularity"] = max(float(existing.get("popularity") or 0), float(incoming.get("popularity") or 0))
    existing["search_text"] = " ".join(filter(None, [existing.get("search_text", ""), incoming.get("search_text", "")]))
    return existing


def ensure_session_id(session_id: str | None = None) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "", _clean_text(session_id))[:80]
    return clean or uuid.uuid4().hex


def load_chat_history(creator_id: str, session_id: str, limit: int = MAX_TURNS) -> list[dict[str, str]]:
    creator = (creator_id or DEFAULT_CREATOR_ID).strip() or DEFAULT_CREATOR_ID
    session = ensure_session_id(session_id)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT turns_json FROM storefront_chat_sessions WHERE creator_id = ? AND session_id = ?",
            (creator, session),
        ).fetchone()
    finally:
        conn.close()
    turns = _loads_list(row["turns_json"]) if row else []
    return turns[-limit:]


def append_chat_turn(
    creator_id: str,
    session_id: str,
    user_message: str,
    assistant_reply: str,
    products: list[dict[str, Any]],
    limit: int = MAX_TURNS,
) -> None:
    creator = (creator_id or DEFAULT_CREATOR_ID).strip() or DEFAULT_CREATOR_ID
    session = ensure_session_id(session_id)
    turns = load_chat_history(creator, session, limit=limit)
    turns.append({
        "user": _clean_text(user_message)[:1000],
        "assistant": _clean_text(assistant_reply)[:1000],
        "products": [
            {
                "id": p.get("id") or p.get("asin") or "",
                "name": p.get("name") or "",
                "network": p.get("network") or "",
            }
            for p in products[:3]
        ],
    })
    turns = turns[-limit:]
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO storefront_chat_sessions (creator_id, session_id, turns_json)
            VALUES (?, ?, ?)
            ON CONFLICT(creator_id, session_id) DO UPDATE SET
                turns_json = EXCLUDED.turns_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (creator, session, json.dumps(turns)),
        )
        conn.commit()
    finally:
        conn.close()


def format_history_for_prompt(history: list[dict[str, Any]]) -> str:
    if not history:
        return "(none)"
    lines = []
    for turn in history[-4:]:
        if turn.get("user"):
            lines.append(f"Shopper: {turn['user']}")
        if turn.get("assistant"):
            product_names = ", ".join(p.get("name", "") for p in (turn.get("products") or []) if p.get("name"))
            suffix = f" Products shown: {product_names}." if product_names else ""
            lines.append(f"Assistant: {turn['assistant']}{suffix}")
    return "\n".join(lines) or "(none)"


def _clicks_by_item(conn, creator_id: str) -> dict[tuple[str, str], int]:
    if not _table_exists(conn, "click_log"):
        return {}
    clicks: dict[tuple[str, str], int] = {}
    queries = [
        (
            """
            SELECT cl.asin AS item_id, COUNT(*) AS c
            FROM click_log cl
            JOIN collages c ON c.slug = cl.slug
            WHERE COALESCE(c.creator_id, ?) = ?
            GROUP BY cl.asin
            """,
            (DEFAULT_CREATOR_ID, creator_id),
        ),
        (
            """
            SELECT cl.asin AS item_id, COUNT(*) AS c
            FROM click_log cl
            JOIN posts p ON p.slug = cl.slug
            WHERE COALESCE(p.creator_id, ?) = ?
            GROUP BY cl.asin
            """,
            (DEFAULT_CREATOR_ID, creator_id),
        ),
    ]
    for sql, params in queries:
        for row in conn.execute(sql, params).fetchall():
            item_id = _clean_text(row["item_id"])
            if item_id:
                clicks[("any", item_id)] = clicks.get(("any", item_id), 0) + int(row["c"] or 0)
    return clicks


def _popularity(product: dict[str, Any], clicks: int = 0) -> float:
    score = float(clicks or 0)
    for key in ("item_count", "sale_amount", "total_earnings"):
        try:
            score += float(product.get(key) or 0)
        except (TypeError, ValueError):
            pass
    try:
        rank = int(product.get("source_rank") or product.get("rank") or 0)
        if rank > 0:
            score += max(0, 100 - rank)
    except (TypeError, ValueError):
        pass
    return score


def retrieve_candidates(
    creator_id: str,
    query: str,
    current_slug: str = "",
    history: list[dict[str, Any]] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search all public product-bearing storefront content for one creator."""
    creator = (creator_id or DEFAULT_CREATOR_ID).strip() or DEFAULT_CREATOR_ID
    current = _clean_text(current_slug).lower()
    history = history or []
    recent_user_context = " ".join(turn.get("user", "") for turn in history[-2:])
    effective_query = " ".join(filter(None, [recent_user_context, query]))

    conn = _connect()
    try:
        click_map = _clicks_by_item(conn, creator)
        by_key: dict[tuple[str, str], dict[str, Any]] = {}

        collage_rows = conn.execute(
            """
            SELECT slug, products_json, caption, hero_title, hero_subtitle, click_count, created_at
            FROM collages
            WHERE COALESCE(creator_id, ?) = ?
              AND COALESCE(status, 'published') = 'published'
            ORDER BY created_at DESC
            """,
            (DEFAULT_CREATOR_ID, creator),
        ).fetchall()
        for row in collage_rows:
            page_text = " ".join([row["slug"] or "", row["caption"] or "", row["hero_title"] or "", row["hero_subtitle"] or ""])
            for product in _loads_list(row["products_json"]):
                if not isinstance(product, dict):
                    continue
                candidate = _candidate_base(product, "collection", page_text)
                if not candidate:
                    continue
                item_id = candidate["id"]
                candidate["collection_slugs"].add(row["slug"] or "")
                clicks = click_map.get(("any", item_id), 0)
                candidate["clicks"] = clicks
                candidate["popularity"] = _popularity(product, clicks) + float(row["click_count"] or 0)
                key = (candidate["network"], item_id)
                by_key[key] = _merge_candidate(by_key[key], candidate) if key in by_key else candidate

        post_rows = conn.execute(
            """
            SELECT slug, asin, network, angle, copy, collection_slug, smart_link,
                   smart_link_affiliate_url, smart_link_final_url,
                   product_name, product_brand, product_price, product_image,
                   product_availability, product_rating, product_review_count, created_at
            FROM posts
            WHERE COALESCE(creator_id, ?) = ?
              AND status IN ('approved', 'posted')
              AND COALESCE(asin, '') != ''
            ORDER BY COALESCE(posted_at, created_at) DESC
            LIMIT 2000
            """,
            (DEFAULT_CREATOR_ID, creator),
        ).fetchall()
        for row in post_rows:
            product = {
                "asin": row["asin"],
                "network": row["network"] or "amazon",
                "product_name": row["product_name"],
                "brand": row["product_brand"],
                "price": row["product_price"],
                "image": row["product_image"],
                "product_availability": row["product_availability"],
                "product_rating": row["product_rating"],
                "product_review_count": row["product_review_count"],
                "smart_link": row["smart_link"] or row["smart_link_final_url"] or row["smart_link_affiliate_url"],
            }
            candidate = _candidate_base(product, "post", row["angle"] or "", row["copy"] or "", row["collection_slug"] or "")
            if not candidate:
                continue
            item_id = candidate["id"]
            if row["slug"]:
                candidate["post_slugs"].add(row["slug"])
            if row["collection_slug"]:
                candidate["collection_slugs"].add(row["collection_slug"])
            clicks = click_map.get(("any", item_id), 0)
            candidate["clicks"] = clicks
            candidate["popularity"] = _popularity(product, clicks)
            key = (candidate["network"], item_id)
            by_key[key] = _merge_candidate(by_key[key], candidate) if key in by_key else candidate
    finally:
        conn.close()

    ranked = rank_candidates(effective_query, list(by_key.values()), current_slug=current)
    return ranked[:limit]


def rank_candidates(query: str, candidates: list[dict[str, Any]], current_slug: str = "") -> list[dict[str, Any]]:
    q_tokens = set(_tokens(query))
    q_text = _clean_text(query).lower()
    current = _clean_text(current_slug).lower()

    def score(candidate: dict[str, Any]) -> tuple[float, float, float, int, str]:
        search_text = _clean_text(candidate.get("search_text")).lower()
        title_tokens = set(_tokens(candidate.get("name")))
        brand_tokens = set(_tokens(candidate.get("brand")))
        category_tokens = set(_tokens(candidate.get("category")))
        all_tokens = set(_tokens(search_text))

        semantic = float(len(q_tokens & all_tokens))
        if q_text and q_text in search_text:
            semantic += 5.0
        if q_tokens and q_tokens.issubset(all_tokens):
            semantic += 3.0

        exact_overlap = float(
            (len(q_tokens & title_tokens) * 3)
            + (len(q_tokens & brand_tokens) * 2)
            + (len(q_tokens & category_tokens) * 2)
        )
        popularity = math.log1p(float(candidate.get("popularity") or 0))
        on_current_page = 1 if current and current in {s.lower() for s in candidate.get("collection_slugs", set())} else 0
        candidate["rank_score"] = semantic + exact_overlap + popularity + (0.25 * on_current_page)
        return (semantic, exact_overlap, popularity, on_current_page, candidate.get("name", ""))

    out = sorted(candidates, key=score, reverse=True)
    for candidate in out:
        candidate["sources"] = sorted(s for s in candidate.get("sources", set()) if s)
        candidate["collection_slugs"] = sorted(s for s in candidate.get("collection_slugs", set()) if s)
        candidate["post_slugs"] = sorted(s for s in candidate.get("post_slugs", set()) if s)
    return out


def format_candidates_for_prompt(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for idx, product in enumerate(candidates):
        lines.append(
            f"[{idx}] {product.get('retailer', '')} {product.get('id', '')} | "
            f"{product.get('name', '')[:120]} | Brand:{product.get('brand', '')} | "
            f"Category:{product.get('category', '')} | Price:{product.get('price', '')} | "
            f"Clicks:{product.get('clicks', 0)} | Sources:{','.join(product.get('sources', []))}"
        )
    return "\n".join(lines)


def parse_product_indexes(raw: str, candidate_count: int, max_items: int = 3) -> tuple[str, list[int]]:
    reply = "Here are a few picks you might love."
    indexes: list[int] = []
    for line in _clean_text(raw).splitlines():
        clean = line.strip()
        if clean.upper().startswith("REPLY:"):
            reply = clean.split(":", 1)[1].strip() or reply
        elif clean.upper().startswith("PRODUCTS:"):
            for bit in clean.split(":", 1)[1].split(","):
                value = bit.strip()
                if value.isdigit():
                    idx = int(value)
                    if 0 <= idx < candidate_count and idx not in indexes:
                        indexes.append(idx)
    return reply, indexes[:max_items]


def response_product(
    candidate: dict[str, Any],
    creator: dict[str, Any],
    current_slug: str = "",
    make_smart_link: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item_id = candidate.get("id") or candidate.get("asin") or ""
    network = candidate.get("network") or "amazon"
    link = _clean_text(candidate.get("link"))

    if not link and network == "amazon" and item_id and make_smart_link:
        try:
            # Canonical agent UTM mapping (plan §5):
            #   utm_source  = 'facebook'         (platform)
            #   utm_medium  = 'agent'            (chat/agent origin, not 'chat')
            #   utm_campaign = collection slug   (or 'agent' fallback)
            #   utm_content = 'agent-recommend'  (origin tag)
            #   utm_term    = ASIN lowercased
            smart = make_smart_link(
                asin=item_id,
                network="amazon",
                utm_source="facebook",
                utm_medium="agent",
                utm_campaign=current_slug or "agent",
                utm_content="agent-recommend",
                utm_term=(item_id or "").lower(),
                creator_id=creator.get("id") or DEFAULT_CREATOR_ID,
            )
            link = _clean_text((smart or {}).get("genius_url") or (smart or {}).get("affiliate_url"))
        except Exception:
            link = ""

    if not link and item_id:
        if network == "walmart":
            link = f"https://www.walmart.com/ip/{item_id}"
        else:
            tag = creator.get("amazon_tag") or "mommymedeals-20"
            link = f"https://www.amazon.com/dp/{item_id}?tag={tag}"

    return {
        "asin": item_id,
        "id": item_id,
        "network": network,
        "retailer": "Walmart" if network == "walmart" else "Amazon",
        "name": candidate.get("name") or item_id,
        "brand": candidate.get("brand") or "",
        "price": candidate.get("price") or "",
        "image": candidate.get("image") or "",
        "availability": candidate.get("availability") or "",
        "rating": candidate.get("rating") or "",
        "review_count": candidate.get("review_count") or "",
        "link": link,
        "sources": candidate.get("sources") or [],
    }
