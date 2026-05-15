"""
Insights data layer — joins click_log × collages × earnings_amazon × attribution_paid.

Phase 2A scope (per Q1/Q2 decisions):
- Click-weighted earnings reconciliation by slug (slug-level, not UTM-tag-level yet)
- Archer paid attribution pulled on-demand on /insights load (no background job)
- Time windows: today / yesterday / 7d / 30d / custom

The route handler in app.py calls these functions and renders insights.html.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import db_schema


def _connect():
    return db_schema._connect()


def _fmt_date(v) -> str:
    """Return YYYY-MM-DD for a datetime object or ISO string; '' for None."""
    if v is None:
        return ''
    if hasattr(v, 'date'):
        return str(v.date())
    return str(v)[:10]


def resolve_window(window: str, custom_start: Optional[str] = None,
                   custom_end: Optional[str] = None) -> tuple[str, str, str]:
    """Return (start_iso, end_iso, label) for a window keyword."""
    today = date.today()
    if window == 'today':
        return today.isoformat(), today.isoformat(), 'Today'
    if window == 'yesterday':
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat(), 'Yesterday'
    if window == '7d':
        return (today - timedelta(days=6)).isoformat(), today.isoformat(), 'Last 7 days'
    if window == '30d':
        return (today - timedelta(days=29)).isoformat(), today.isoformat(), 'Last 30 days'
    if window == 'custom':
        s = custom_start or today.isoformat()
        e = custom_end or today.isoformat()
        return s, e, f'{s} → {e}'
    # Default fallback
    return (today - timedelta(days=29)).isoformat(), today.isoformat(), 'Last 30 days'


def _date_filter(start: str, end: str, col: str = 'clicked_at') -> tuple[str, list]:
    """Build a 'BETWEEN ? AND ?' filter inclusive on both ends.

    Inclusive end means we use end + 1 day excluded so a same-day window still works.
    """
    end_dt = (datetime.fromisoformat(end) + timedelta(days=1)).date().isoformat()
    return f"DATE({col}) >= ? AND DATE({col}) < ?", [start, end_dt]


def _safe_json_list(s: Optional[str]) -> list:
    try:
        v = json.loads(s) if s else []
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def collections_summary(creator_id: str, start: str, end: str) -> list[dict]:
    """Per-collection: clicks, organic/paid badges, click-weighted estimated revenue,
    top products inside.

    Click-weighted earnings reconciliation:
    For each ASIN with earnings rows whose period overlaps the query window, sum
    earnings → distribute proportionally across collections that drove a click on
    that ASIN inside the window, weighted by click counts.
    """
    conn = _connect()
    where, params = _date_filter(start, end, 'clicked_at')

    # 1) Click counts per (slug, asin) inside the window
    rows = conn.execute(
        f"SELECT slug, asin, COUNT(*) as clicks "
        f"FROM click_log "
        f"WHERE {where} AND slug IS NOT NULL AND slug != '' "
        f"GROUP BY slug, asin",
        params,
    ).fetchall()

    clicks_by_slug: dict[str, dict] = {}
    asin_total_clicks: dict[str, int] = {}
    for r in rows:
        slug, asin, clicks = r['slug'], r['asin'], int(r['clicks'] or 0)
        bucket = clicks_by_slug.setdefault(slug, {'total': 0, 'asins': {}})
        bucket['total'] += clicks
        bucket['asins'][asin] = bucket['asins'].get(asin, 0) + clicks
        asin_total_clicks[asin] = asin_total_clicks.get(asin, 0) + clicks

    # 2) Earnings per ASIN inside the window (from earnings_amazon)
    earnings_rows = conn.execute(
        "SELECT asin, SUM(earnings) AS earnings, SUM(units) AS units "
        "FROM earnings_amazon "
        "WHERE creator_id = ? "
        "  AND DATE(period_start) <= ? AND DATE(period_end) >= ? "
        "GROUP BY asin",
        (creator_id, end, start),
    ).fetchall()
    earnings_by_asin = {r['asin']: float(r['earnings'] or 0) for r in earnings_rows}

    # 3) Collection metadata — only this creator's collections that have at least
    #    one click in the window OR were created in the window
    slug_meta_rows = conn.execute(
        "SELECT slug, hero_title, theme, products_json, status, "
        "       campaign_types, created_at, click_count "
        "FROM collages "
        "WHERE COALESCE(creator_id, 'everydaywithsteph') = ? "
        "  AND COALESCE(status, 'published') != 'archived'",
        (creator_id,),
    ).fetchall()
    meta_by_slug = {r['slug']: r for r in slug_meta_rows}

    # 4) Compose summary, click-weighted revenue per slug
    out = []
    seen_slugs = set(clicks_by_slug.keys()) | set(meta_by_slug.keys())
    for slug in seen_slugs:
        meta = meta_by_slug.get(slug)
        bucket = clicks_by_slug.get(slug, {'total': 0, 'asins': {}})
        # Estimated revenue: for each ASIN that fired in this collection, take
        # the share of total clicks on that ASIN that came through this slug.
        est_rev = 0.0
        top_products = []
        for asin, asin_clicks in bucket['asins'].items():
            denom = asin_total_clicks.get(asin, 0) or 1
            asin_earn = earnings_by_asin.get(asin, 0.0)
            share = asin_clicks / denom
            est_rev += asin_earn * share
            top_products.append({
                'asin':    asin,
                'clicks':  asin_clicks,
                'est_rev': round(asin_earn * share, 2),
            })
        top_products.sort(key=lambda p: p['clicks'], reverse=True)

        if meta:
            campaign_types = _safe_json_list(meta['campaign_types']) or ['organic']
            title = meta['hero_title'] or (meta['slug'] or '').replace('-', ' ').title()
            theme = meta['theme'] or 'coral'
            status = meta['status'] or 'published'
            created_at = _fmt_date(meta['created_at'])
        else:
            campaign_types = ['organic']
            title = slug.replace('-', ' ').title()
            theme = 'coral'
            status = '(orphan click_log)'
            created_at = ''

        out.append({
            'slug':           slug,
            'title':          title,
            'theme':          theme,
            'status':         status,
            'campaign_types': campaign_types,
            'clicks':         bucket['total'],
            'est_revenue':    round(est_rev, 2),
            'top_products':   top_products[:5],
            'created_at':     created_at,
        })

    out.sort(key=lambda r: (r['clicks'], r['est_revenue']), reverse=True)
    conn.close()
    return out


def posts_summary(creator_id: str, start: str, end: str) -> list[dict]:
    """Per-Mode-B-post performance.

    Branch 2B: joins the `posts` table (persisted Mode B output) against
    `click_log` by slug. Posts that haven't received any clicks still show up
    so creators can see their content backlog and approval status. Posts
    created within the window are included even with zero clicks.
    """
    conn = _connect()
    where_clicks, click_params = _date_filter(start, end, 'clicked_at')

    # 1) Click counts per (post.slug, asin) inside the window
    click_rows = conn.execute(
        f"SELECT slug, COUNT(*) AS clicks, MAX(clicked_at) AS last_click "
        f"FROM click_log "
        f"WHERE {where_clicks} AND slug IS NOT NULL AND slug != '' "
        f"GROUP BY slug",
        click_params,
    ).fetchall()
    clicks_by_slug = {r['slug']: dict(r) for r in click_rows}

    # 2) Posts for this creator created OR updated in the window. (Plus any
    #    clicked-in-window posts that were created earlier — folded in below.)
    posts_where_c, posts_params_c = _date_filter(start, end, 'created_at')
    posts_where_u, posts_params_u = _date_filter(start, end, 'updated_at')
    post_rows = conn.execute(
        f"SELECT id, slug, asin, angle, status, copy, collection_slug, "
        f"       product_name, product_brand, created_at, posted_at "
        f"FROM posts "
        f"WHERE COALESCE(creator_id,'everydaywithsteph') = ? "
        f"  AND status != 'archived' "
        f"  AND (({posts_where_c}) OR ({posts_where_u}))",
        [creator_id, *posts_params_c, *posts_params_u],
    ).fetchall()

    # 2b) Pull in older posts whose slug got clicks during the window
    seen_ids = {r['id'] for r in post_rows}
    if clicks_by_slug:
        slug_placeholders = ','.join('?' for _ in clicks_by_slug)
        extra_rows = conn.execute(
            f"SELECT id, slug, asin, angle, status, copy, collection_slug, "
            f"       product_name, product_brand, created_at, posted_at "
            f"FROM posts "
            f"WHERE COALESCE(creator_id,'everydaywithsteph') = ? "
            f"  AND status != 'archived' "
            f"  AND slug IN ({slug_placeholders})",
            [creator_id, *list(clicks_by_slug.keys())],
        ).fetchall()
        post_rows = list(post_rows) + [r for r in extra_rows if r['id'] not in seen_ids]

    conn.close()

    out = []
    for r in post_rows:
        slug = r['slug'] or ''
        click_info = clicks_by_slug.get(slug, {})
        out.append({
            'id':              r['id'],
            'slug':            slug,
            'angle':           r['angle'] or '',
            'asin':            r['asin'] or '',
            'product_name':    r['product_name'] or '',
            'product_brand':   r['product_brand'] or '',
            'status':          r['status'] or 'draft',
            'collection_slug': r['collection_slug'] or '',
            'clicks':          int(click_info.get('clicks') or 0),
            'last_click':      _fmt_date(click_info.get('last_click')),
            'created_at':      _fmt_date(r['created_at']),
            'posted_at':       _fmt_date(r['posted_at']),
        })
    out.sort(key=lambda r: (r['clicks'], r['created_at']), reverse=True)
    return out


def ads_summary(creator_id: str, start: str, end: str,
                pull_archer_now: bool = True) -> list[dict]:
    """Per-ad-layer paid attribution.

    Pulls Archer attribution data on-demand. The Archer client (product_api.py)
    is best-effort — if no key/auth, we fall back to whatever's already in
    attribution_paid (last cached pull).
    """
    if pull_archer_now:
        try:
            _refresh_archer_attribution(creator_id, start, end)
        except Exception as e:
            logging.warning(f"[INSIGHTS] Archer pull failed: {e}")

    conn = _connect()
    rows = conn.execute(
        "SELECT label, network, "
        "       SUM(clicks) AS clicks, "
        "       SUM(conversions) AS conversions, "
        "       SUM(revenue) AS revenue, "
        "       MAX(pulled_at) AS pulled_at "
        "FROM attribution_paid "
        "WHERE creator_id = ? "
        "  AND DATE(period_start) <= ? AND DATE(period_end) >= ? "
        "GROUP BY label, network "
        "ORDER BY clicks DESC",
        (creator_id, end, start),
    ).fetchall()
    conn.close()
    return [
        {
            'label':       r['label'],
            'network':     r['network'],
            'clicks':      int(r['clicks'] or 0),
            'conversions': int(r['conversions'] or 0),
            'revenue':     float(r['revenue'] or 0),
            'pulled_at':   r['pulled_at'],
        }
        for r in rows
    ]


def _refresh_archer_attribution(creator_id: str, start: str, end: str) -> None:
    """Best-effort Archer attribution pull. No-op if API not available.

    Phase 2A keeps this minimal — we record an empty pull row with the current
    timestamp so the /insights UI shows when the last attempt happened. When
    the real Archer attribution endpoint is wired up later it slots in here.
    """
    # Placeholder: real implementation would call ArcherAPI to fetch label-level
    # attribution data and INSERT into attribution_paid. For now we stub a touch
    # so the "last pulled" timestamp surfaces.
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO attribution_paid "
            "(creator_id, network, label, clicks, conversions, revenue, "
            " period_start, period_end) "
            "VALUES (?, 'archer', '_pull_marker', 0, 0, 0, ?, ?)",
            (creator_id, start, end),
        )
        conn.commit()
    finally:
        conn.close()


def overview(creator_id: str, start: str, end: str) -> dict:
    """Top-line numbers for the insights header strip."""
    conn = _connect()
    where, params = _date_filter(start, end, 'clicked_at')
    total_clicks = conn.execute(
        f"SELECT COUNT(*) AS n FROM click_log WHERE {where}",
        params,
    ).fetchone()['n'] or 0

    total_earnings_row = conn.execute(
        "SELECT COALESCE(SUM(earnings),0) AS s FROM earnings_amazon "
        "WHERE creator_id = ? "
        "  AND DATE(period_start) <= ? AND DATE(period_end) >= ?",
        (creator_id, end, start),
    ).fetchone()
    total_earnings = float(total_earnings_row['s'] or 0)

    published_count = conn.execute(
        "SELECT COUNT(*) AS n FROM collages "
        "WHERE COALESCE(creator_id,'everydaywithsteph') = ? "
        "  AND COALESCE(status,'published') = 'published'",
        (creator_id,),
    ).fetchone()['n'] or 0

    conn.close()
    return {
        'total_clicks':    int(total_clicks),
        'total_earnings':  round(total_earnings, 2),
        'published_count': int(published_count),
    }
