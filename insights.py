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


# ── P0.2 v2 ranking helpers ─────────────────────────────────────────────────
# Steph's S4 spec (R16): 4 scrolling sections — Best Collections by clicks,
# Best Posts by clicks, Best Products by earnings, Best Retailers by earnings.
# Each helper returns a list of dicts, sorted desc by the section's primary
# metric. Secondary metric ("views") is intentionally left as None on every row
# — no impressions source data exists today (tracked in issue #87).
#
# All four helpers:
#   - take (creator_id, start, end) and scope by creator
#   - are paramstyle-agnostic (work under both PG and the SQLite dev fallback)
#   - return [] cleanly on missing tables / missing columns / empty windows
#   - never raise into the route handler
#
# click_log has no creator_id column; click-based helpers scope through the
# join target (collages.creator_id, posts.creator_id). This matches v1
# semantics and is documented inline so anyone tightening it later sees the
# intent.

def _table_exists(conn, name: str) -> bool:
    """True if `name` is a queryable table in the current DB connection.

    Tries both SQLite (sqlite_master) and PG (information_schema) lookups; the
    PG-compat wrapper in db_schema._adapt_sql doesn't know how to translate
    sqlite_master, so we probe in two passes and swallow any error.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        if row is not None:
            return True
    except Exception:
        pass
    try:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def collections_ranked(creator_id: str, start: str, end: str) -> list[dict]:
    """Best Collections by click traffic (R16 section 1).

    Joins click_log × collages on slug, scoped via collages.creator_id.
    Returns rows sorted by clicks desc. `views` is None — no impressions data
    source exists yet (issue #87).
    """
    if not creator_id:
        return []
    conn = _connect()
    try:
        if not (_table_exists(conn, 'click_log') and _table_exists(conn, 'collages')):
            return []
        where, params = _date_filter(start, end, 'cl.clicked_at')
        rows = conn.execute(
            f"SELECT c.slug AS slug, "
            f"       c.hero_title AS hero_title, "
            f"       c.theme AS theme, "
            f"       COUNT(cl.id) AS clicks "
            f"FROM collages c "
            f"LEFT JOIN click_log cl ON cl.slug = c.slug AND ({where}) "
            f"WHERE COALESCE(c.creator_id, 'everydaywithsteph') = ? "
            f"  AND COALESCE(c.status, 'published') != 'archived' "
            f"GROUP BY c.slug, c.hero_title, c.theme "
            f"ORDER BY clicks DESC, c.slug ASC",
            [*params, creator_id],
        ).fetchall()
        return [
            {
                'slug':   r['slug'],
                'title':  r['hero_title'] or (r['slug'] or '').replace('-', ' ').title(),
                'theme':  r['theme'] or 'coral',
                'clicks': int(r['clicks'] or 0),
                'views':  None,  # issue #87: no impressions source data yet
            }
            for r in rows
        ]
    except Exception as e:
        logging.warning(f"[INSIGHTS] collections_ranked failed: {e}")
        return []
    finally:
        conn.close()


def posts_ranked(creator_id: str, start: str, end: str) -> list[dict]:
    """Best Posts by click traffic (R16 section 2).

    Joins click_log × posts on slug, scoped via posts.creator_id. posts.slug
    and collages.slug share the click_log.slug join key by design — clicks
    on a slug that's both a collection landing AND a post page are counted
    in both sections. Matches v1 semantics.
    """
    if not creator_id:
        return []
    conn = _connect()
    try:
        if not (_table_exists(conn, 'click_log') and _table_exists(conn, 'posts')):
            return []
        where, params = _date_filter(start, end, 'cl.clicked_at')
        rows = conn.execute(
            f"SELECT p.id AS id, "
            f"       p.slug AS slug, "
            f"       p.product_name AS product_name, "
            f"       p.angle AS angle, "
            f"       p.collection_slug AS collection_slug, "
            f"       p.status AS status, "
            f"       COUNT(cl.id) AS clicks "
            f"FROM posts p "
            f"LEFT JOIN click_log cl ON cl.slug = p.slug AND ({where}) "
            f"WHERE COALESCE(p.creator_id, 'everydaywithsteph') = ? "
            f"  AND COALESCE(p.status, 'draft') != 'archived' "
            f"  AND p.slug IS NOT NULL AND p.slug != '' "
            f"GROUP BY p.id, p.slug, p.product_name, p.angle, p.collection_slug, p.status "
            f"ORDER BY clicks DESC, p.id ASC",
            [*params, creator_id],
        ).fetchall()
        return [
            {
                'id':              r['id'],
                'slug':            r['slug'],
                'title':           r['product_name'] or r['slug'],
                'angle':           r['angle'] or '',
                'collection_slug': r['collection_slug'] or '',
                'status':          r['status'] or 'draft',
                'clicks':          int(r['clicks'] or 0),
                'views':           None,  # issue #87
            }
            for r in rows
        ]
    except Exception as e:
        logging.warning(f"[INSIGHTS] posts_ranked failed: {e}")
        return []
    finally:
        conn.close()


def products_ranked(creator_id: str, start: str, end: str) -> list[dict]:
    """Best Products by earnings (R16 section 3).

    Sums earnings_amazon rows whose period overlaps the window, grouped by
    ASIN. Scoped by creator_id directly (earnings_amazon has the column).
    """
    if not creator_id:
        return []
    conn = _connect()
    try:
        if not _table_exists(conn, 'earnings_amazon'):
            return []
        rows = conn.execute(
            "SELECT asin, "
            "       MAX(product_name) AS product_name, "
            "       COALESCE(SUM(earnings), 0) AS earnings, "
            "       COALESCE(SUM(units), 0) AS units "
            "FROM earnings_amazon "
            "WHERE creator_id = ? "
            "  AND DATE(period_start) <= ? AND DATE(period_end) >= ? "
            "GROUP BY asin "
            "ORDER BY earnings DESC, asin ASC",
            (creator_id, end, start),
        ).fetchall()
        return [
            {
                'asin':         r['asin'],
                'product_name': r['product_name'] or r['asin'],
                'earnings':     round(float(r['earnings'] or 0), 2),
                'units':        int(r['units'] or 0),
                'views':        None,  # issue #87
            }
            for r in rows
        ]
    except Exception as e:
        logging.warning(f"[INSIGHTS] products_ranked failed: {e}")
        return []
    finally:
        conn.close()


def retailers_ranked(creator_id: str, start: str, end: str) -> list[dict]:
    """Best Retailers by earnings (R16 section 4).

    PR-1 scope: Amazon-only single row aggregating earnings_amazon for the
    window. No per-creator Walmart earnings table exists today; once one
    lands (P0.4 follow-on or a dedicated Walmart-creator-earnings schema),
    add a second branch here and order by earnings desc.
    """
    if not creator_id:
        return []
    conn = _connect()
    try:
        if not _table_exists(conn, 'earnings_amazon'):
            return []
        row = conn.execute(
            "SELECT COALESCE(SUM(earnings), 0) AS earnings, "
            "       COALESCE(SUM(units), 0) AS units "
            "FROM earnings_amazon "
            "WHERE creator_id = ? "
            "  AND DATE(period_start) <= ? AND DATE(period_end) >= ?",
            (creator_id, end, start),
        ).fetchone()
        earnings = float(row['earnings'] or 0) if row else 0.0
        units = int(row['units'] or 0) if row else 0
        if earnings == 0 and units == 0:
            return []
        # TODO: when per-creator Walmart earnings land, query that table and
        # append a {'retailer': 'Walmart', ...} row, then re-sort desc.
        return [
            {
                'retailer': 'Amazon',
                'earnings': round(earnings, 2),
                'units':    units,
                'views':    None,  # issue #87
            }
        ]
    except Exception as e:
        logging.warning(f"[INSIGHTS] retailers_ranked failed: {e}")
        return []
    finally:
        conn.close()
