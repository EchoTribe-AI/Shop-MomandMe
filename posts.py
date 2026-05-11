"""
Posts data layer — persistence for Mode B individual social posts.

Lifecycle:
  draft     → Claude just generated it, not yet reviewed
  approved  → creator approved, ready to copy/paste into Meta organic
  posted    → marked as published on social (manual mark — we don't auto-detect)
  archived  → soft-deleted, hidden from queue

Each post is product-scoped and may optionally belong to a collection (so the
same set of products can drive 1 collection landing page + N individual posts).

Slug convention:
  post_slug = '{angle-slug}-{asin}-{post_id}'
  Used as the click_log slug so /insights can join posts.id ↔ click_log
  ↔ earnings_amazon and surface per-post performance.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Optional

import db_schema


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_schema.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _slugify_angle(angle: str) -> str:
    s = (angle or 'post').lower().strip()
    s = ''.join(c if c.isalnum() or c == '-' else '-' for c in s)
    while '--' in s:
        s = s.replace('--', '-')
    return s.strip('-')[:30] or 'post'


def create_post(
    creator_id: str,
    asin: str,
    angle: str,
    copy: str,
    image_note: str = '',
    network: str = 'amazon',
    collection_slug: Optional[str] = None,
    status: str = 'draft',
    utm: Optional[dict] = None,
    smart_link: str = '',
    smart_link_id: str = '',
    smart_link_affiliate_url: str = '',
    smart_link_final_url: str = '',
    product_name: str = '',
    product_brand: str = '',
    product_price: str = '',
    product_image: str = '',
    product_availability: str = '',
    product_rating=None,
    product_review_count=None,
) -> dict:
    """Insert a new post and return the saved row with its assigned id + slug."""
    utm = utm or {}
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO posts
               (creator_id, asin, network, angle, copy, image_note,
                collection_slug, status,
                utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                smart_link, smart_link_id, smart_link_affiliate_url,
                smart_link_final_url,
                product_name, product_brand, product_price, product_image,
                product_availability, product_rating, product_review_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                creator_id, asin, network, angle, copy, image_note,
                collection_slug, status,
                utm.get('source', ''), utm.get('medium', ''),
                utm.get('campaign', ''), utm.get('content', ''),
                utm.get('term', ''),
                smart_link, smart_link_id, smart_link_affiliate_url,
                smart_link_final_url,
                product_name, product_brand, product_price, product_image,
                product_availability, product_rating, product_review_count,
            ),
        )
        post_id = cur.lastrowid
        # Generate stable post slug now that we have the id
        slug = f"{_slugify_angle(angle)}-{(asin or 'noasin').lower()}-{post_id}"
        conn.execute(
            "UPDATE posts SET slug = ? WHERE id = ?", (slug, post_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_posts(
    creator_id: Optional[str] = 'everydaywithsteph',
    status: Optional[str] = None,
    collection_slug: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Query posts with optional filters. Default excludes archived."""
    where = ['COALESCE(creator_id,\'everydaywithsteph\') = ?']
    params: list = [creator_id or 'everydaywithsteph']
    if status:
        where.append('status = ?')
        params.append(status)
    else:
        # Default: hide archived
        where.append("status != 'archived'")
    if collection_slug:
        where.append('collection_slug = ?')
        params.append(collection_slug)
    sql = (
        "SELECT * FROM posts "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC "
        f"LIMIT {int(limit)}"
    )
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_post(post_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_post(post_id: int, fields: dict) -> Optional[dict]:
    """Update specific fields. Bumps updated_at automatically.

    Allowed fields: copy, image_note, angle, status, smart_link,
    smart_link_id, smart_link_affiliate_url, smart_link_final_url,
    utm_source, utm_medium, utm_campaign, utm_content, utm_term,
    collection_slug, product_image, posted_at, product_name,
    product_brand, product_price, product_availability, product_rating,
    product_review_count.
    """
    allowed = {
        'copy', 'image_note', 'angle', 'status', 'smart_link',
        'smart_link_id', 'smart_link_affiliate_url', 'smart_link_final_url',
        'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
        'collection_slug', 'product_image', 'posted_at',
        'product_name', 'product_brand', 'product_price',
        'product_availability', 'product_rating', 'product_review_count',
    }
    sets = []
    vals: list = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return get_post(post_id)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(post_id)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE posts SET {', '.join(sets)} WHERE id = ?", vals
        )
        conn.commit()
    finally:
        conn.close()
    return get_post(post_id)


def bulk_set_status(post_ids: list[int], status: str) -> int:
    """Mark many posts at once. Returns count updated."""
    if not post_ids:
        return 0
    placeholders = ','.join('?' for _ in post_ids)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE posts SET status = ?, updated_at = CURRENT_TIMESTAMP, "
            f"  posted_at = CASE WHEN ? = 'posted' THEN CURRENT_TIMESTAMP ELSE posted_at END "
            f"WHERE id IN ({placeholders})",
            [status, status, *post_ids],
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_post(post_id: int) -> bool:
    """Hard delete (use bulk_set_status with 'archived' for soft delete)."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def stats(creator_id: str = 'everydaywithsteph') -> dict:
    """Counts by status — feeds the queue header strip."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM posts "
            "WHERE COALESCE(creator_id,'everydaywithsteph') = ? "
            "GROUP BY status",
            (creator_id,),
        ).fetchall()
        return {r['status']: r['n'] for r in rows}
    finally:
        conn.close()
