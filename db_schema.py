"""
Multi-creator + insights schema — PostgreSQL (psycopg2) backend.

Migrated from SQLite to PostgreSQL for persistent data across Replit
Autoscale deploys. Falls back to SQLite only when DATABASE_URL is unset
(local dev / unit tests without a provisioned DB).

Public API (unchanged for all callers):
  _connect()            → connection-like object (PGConn or sqlite3.Connection)
  _last_id(cur)         → int  — use after INSERT ... RETURNING id
  bootstrap()           → idempotent boot initialiser
  init_schema()         → create / patch all tables
  seed_default_creator()
  get_creator(id)
  list_creators()
  upsert_creator(dict)
  add_campaign_type_to_collage(slug, type)
"""
from __future__ import annotations

import json
import logging
import os
import re as _re

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.extensions
    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False

DATABASE_URL = os.environ.get('DATABASE_URL', '')
_USE_PG = bool(DATABASE_URL) and _HAS_PSYCOPG2

# Legacy path kept so any code that still imports DB_PATH doesn't crash.
DB_PATH = os.environ.get('CACHE_DB_PATH', 'data/archer_catalog.db')

# DDL fragment: auto-increment primary key — differs between PG and SQLite.
_PK = "SERIAL PRIMARY KEY" if _USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

DEFAULT_CREATOR = {
    'id':                 'everydaywithsteph',
    'display_name':       'Steph',
    'handle':             '@EverydaywithSteph',
    'brand_label':        'Mommy & Me Collective',
    'fb_pixel_id':        os.environ.get('FB_PIXEL_ID', '1559451780790812'),
    'fb_page_id':         '100065251532225',
    'amazon_tag':         'mommymedeals-20',
    'meta_ad_account_id': 'act_573934886369270',
    'ltk_url':            'https://shopltk.com/EverydaywithSteph',
    'facebook_url':       'https://www.facebook.com/TheMommyandMeCollective',
    'voice_prompt':       (
        "You are Steph, the creator behind @EverydaywithSteph and the Mommy & Me "
        "Collective. You talk mom-to-mom: warm, enthusiastic, concise, and "
        "occasionally use light emojis (but not excessively). You share deals "
        "and product recommendations like a trusted friend who happens to know "
        "every sale happening right now."
    ),
    'theme_default':      'coral',
    'defaults_json':      json.dumps({}),
}


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL compatibility wrapper
# ─────────────────────────────────────────────────────────────────────────────

import datetime as _dt


_DATETIME_NOW_RE = _re.compile(
    r"""datetime\(\s*'now'\s*(?:,\s*'(?P<sign>[+-]?)\s*(?P<num>\d+)\s+(?P<unit>second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s*'\s*)?\)""",
    _re.IGNORECASE,
)


def _adapt_sql(sql: str) -> str:
    """Adapt SQLite-flavoured SQL to PostgreSQL.

    Handles three differences transparently so callers can keep their
    sqlite3-style query strings without per-backend branching:

      1. `?` placeholders → `%s` (psycopg2 paramstyle).
      2. `datetime('now')` and `datetime('now', '<+|-N unit>')` →
         `NOW()` / `NOW() ± INTERVAL 'N unit'`.
      3. `BEGIN IMMEDIATE` → `SELECT 1` no-op. PostgreSQL is implicitly
         in a transaction whenever a statement runs; SQLite's exclusive
         lock has no direct PG equivalent. Real row locking should use
         explicit `SELECT ... FOR UPDATE` on the rows that matter.
    """
    # 1. BEGIN IMMEDIATE alone — swap for a no-op so callers don't crash.
    if _re.match(r'^\s*BEGIN\s+IMMEDIATE\s*$', sql, _re.IGNORECASE):
        return "SELECT 1"
    # 2. datetime('now') and datetime('now', '-2 hours') and friends.
    def _dt_sub(m):
        if not m.group('num'):
            return "NOW()"
        sign = (m.group('sign') or '').strip() or '+'
        op = '-' if sign == '-' else '+'
        return f"(NOW() {op} INTERVAL '{m.group('num')} {m.group('unit')}')"
    sql = _DATETIME_NOW_RE.sub(_dt_sub, sql)
    # 3. ? → %s. Done last so we don't accidentally translate ?-shaped
    #    artifacts that might appear in earlier replacements.
    return _re.sub(r'\?', '%s', sql)


def _coerce_row(row):
    """
    Convert datetime/date values in a psycopg2 RealDictRow to ISO strings,
    matching the string-based output that sqlite3 always returned.
    Returns None unchanged.
    """
    if row is None:
        return None
    result = {}
    for k, v in row.items():
        if isinstance(v, _dt.datetime):
            result[k] = v.isoformat(sep=' ', timespec='seconds')
        elif isinstance(v, _dt.date):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


class _DateAwareCursor:
    """
    Wraps a psycopg2 RealDictCursor so that fetchone/fetchall always return
    datetime/date values as ISO strings, exactly like sqlite3 does.
    """

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return _coerce_row(self._cur.fetchone())

    def fetchall(self):
        return [_coerce_row(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return getattr(self._cur, 'lastrowid', None)

    def __iter__(self):
        for row in self._cur:
            yield _coerce_row(row)


class _PGConn:
    """
    Thin psycopg2 wrapper that mimics the sqlite3.Connection.execute() API so
    every module in the codebase can keep using conn.execute(sql, params)
    without change.

    Key behaviours:
    - .execute(sql, params) → RealDictCursor (rows are dicts)
    - .row_factory = anything → silently ignored (rows are always dict-like)
    - .in_transaction → bool
    - .commit() / .rollback() / .close() → delegate to underlying PG connection
    - ? placeholders are auto-converted to %s before execution
    """

    def __init__(self, pg_conn):
        self._conn = pg_conn

    # sqlite3 compat: accept row_factory assignments without error
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass

    @property
    def in_transaction(self) -> bool:
        try:
            return self._conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION
        except Exception:
            return False

    def execute(self, sql: str, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_adapt_sql(sql), params if params is not None else ())
        return _DateAwareCursor(cur)

    def executemany(self, sql: str, seq_of_params):
        cur = self._conn.cursor()
        for params in seq_of_params:
            cur.execute(_adapt_sql(sql), params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        """
        Close the underlying connection.  Any uncommitted work is rolled back
        implicitly by psycopg2, preserving explicit-commit semantics.
        Callers must call .commit() themselves before .close() to persist data.
        """
        try:
            self._conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Connection factory
# ─────────────────────────────────────────────────────────────────────────────

def _connect(timeout: int = 30):
    """
    Open a database connection.

    Returns a _PGConn (psycopg2 wrapper) when DATABASE_URL is set,
    otherwise a native sqlite3.Connection for local dev / unit tests.
    """
    if _USE_PG:
        pg = psycopg2.connect(DATABASE_URL, connect_timeout=timeout)
        pg.autocommit = False
        return _PGConn(pg)
    # ── SQLite fallback ────────────────────────────────────────────────────
    import sqlite3
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _last_id(cur) -> int:
    """
    Return the id assigned by the most recent INSERT ... RETURNING id.

    Works for both psycopg2 RealDictCursor (fetchone returns a dict-like row)
    and native sqlite3 cursors (uses lastrowid as fallback).
    """
    try:
        row = cur.fetchone()
        if row is not None:
            if hasattr(row, 'get'):
                return int(row.get('id') or row.get('id', 0))
            return int(row[0])
    except Exception:
        pass
    try:
        return int(cur.lastrowid or 0)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────────────────────────

def _add_column_if_missing(conn, table: str, col_def: str) -> None:
    """ALTER TABLE … ADD COLUMN if the column doesn't exist yet. Idempotent."""
    col_name = col_def.split()[0]
    if _USE_PG:
        row = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, col_name),
        ).fetchone()
        if not row:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    else:
        import sqlite3
        cur = conn.execute(f"PRAGMA table_info({table})")
        existing = {r[1] for r in cur.fetchall()}
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")


# ─────────────────────────────────────────────────────────────────────────────
# Schema init
# ─────────────────────────────────────────────────────────────────────────────

def init_schema() -> None:
    """Create new tables and patch existing columns. Idempotent."""
    conn = _connect()
    try:
        # ── creators ──────────────────────────────────────────────────────
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS creators (
                id                 TEXT PRIMARY KEY,
                display_name       TEXT NOT NULL,
                handle             TEXT,
                brand_label        TEXT,
                fb_pixel_id        TEXT,
                amazon_tag         TEXT,
                meta_ad_account_id TEXT,
                ltk_url            TEXT,
                facebook_url       TEXT,
                voice_prompt       TEXT,
                theme_default      TEXT DEFAULT 'coral',
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _add_column_if_missing(conn, 'creators', "fb_page_id TEXT")
        _add_column_if_missing(conn, 'creators', "defaults_json TEXT DEFAULT '{}'")

        # ── earnings_amazon ───────────────────────────────────────────────
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS earnings_amazon (
                id           {_PK},
                creator_id   TEXT NOT NULL,
                asin         TEXT NOT NULL,
                product_name TEXT,
                period_start DATE,
                period_end   DATE,
                earnings     REAL DEFAULT 0,
                units        INTEGER DEFAULT 0,
                source_file  TEXT,
                uploaded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_earnings_amazon_asin ON earnings_amazon(asin)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_earnings_amazon_creator ON earnings_amazon(creator_id)")

        # ── attribution_paid ──────────────────────────────────────────────
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS attribution_paid (
                id           {_PK},
                creator_id   TEXT NOT NULL,
                network      TEXT NOT NULL,
                label        TEXT,
                clicks       INTEGER DEFAULT 0,
                conversions  INTEGER DEFAULT 0,
                revenue      REAL DEFAULT 0,
                period_start DATE,
                period_end   DATE,
                pulled_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attribution_label ON attribution_paid(label)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attribution_creator ON attribution_paid(creator_id)")

        # ── collages ──────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collages (
                slug TEXT PRIMARY KEY,
                products_json TEXT,
                layout TEXT DEFAULT 'layout-2',
                theme TEXT DEFAULT 'coral',
                caption TEXT,
                direct_to_amazon INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                click_count INTEGER DEFAULT 0
            )
        """)
        _add_column_if_missing(conn, 'collages', "creator_id TEXT DEFAULT 'everydaywithsteph'")
        _add_column_if_missing(conn, 'collages', "status TEXT DEFAULT 'published'")
        _add_column_if_missing(conn, 'collages', "campaign_types TEXT DEFAULT '[\"organic\"]'")
        _add_column_if_missing(conn, 'collages', "hero_title TEXT")
        _add_column_if_missing(conn, 'collages', "hero_subtitle TEXT")

        # ── posts ─────────────────────────────────────────────────────────
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS posts (
                id              {_PK},
                creator_id      TEXT NOT NULL DEFAULT 'everydaywithsteph',
                asin            TEXT,
                network         TEXT DEFAULT 'amazon',
                angle           TEXT,
                copy            TEXT,
                image_note      TEXT,
                collection_slug TEXT,
                status          TEXT DEFAULT 'draft',
                utm_source      TEXT,
                utm_medium      TEXT,
                utm_campaign    TEXT,
                utm_content     TEXT,
                utm_term        TEXT,
                smart_link      TEXT,
                smart_link_id   TEXT,
                smart_link_affiliate_url TEXT,
                smart_link_final_url TEXT,
                product_name    TEXT,
                product_brand   TEXT,
                product_price   TEXT,
                product_image   TEXT,
                slug            TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                posted_at       TIMESTAMP
            )
        """)
        _add_column_if_missing(conn, 'posts', "smart_link_id TEXT")
        _add_column_if_missing(conn, 'posts', "smart_link_affiliate_url TEXT")
        _add_column_if_missing(conn, 'posts', "smart_link_final_url TEXT")
        _add_column_if_missing(conn, 'posts', "product_availability TEXT")
        _add_column_if_missing(conn, 'posts', "product_rating REAL")
        _add_column_if_missing(conn, 'posts', "product_review_count INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_creator ON posts(creator_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_collection ON posts(collection_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_slug ON posts(slug)")

        # ── storefront_chat_sessions ──────────────────────────────────────
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS storefront_chat_sessions (
                id          {_PK},
                creator_id  TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                turns_json  TEXT DEFAULT '[]',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(creator_id, session_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_storefront_chat_creator_session "
            "ON storefront_chat_sessions(creator_id, session_id)"
        )

        # ── campaigns_v3 ──────────────────────────────────────────────────
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS campaigns_v3 (
                id                       {_PK},
                creator_id               TEXT NOT NULL DEFAULT 'everydaywithsteph',
                package_type             TEXT NOT NULL,
                target_type              TEXT NOT NULL,
                target_value             TEXT NOT NULL,
                brand_slug               TEXT,
                product_slug             TEXT,
                product_name             TEXT,
                destination_url          TEXT,
                layers_json              TEXT,
                asset_url                TEXT,
                asset_type               TEXT DEFAULT 'static_image',
                package_json             TEXT NOT NULL,
                defaults_overrides_json  TEXT,
                utm_auto                 INTEGER DEFAULT 1,
                status                   TEXT DEFAULT 'draft',
                meta_campaign_ids_json   TEXT,
                meta_post_id             TEXT,
                notes                    TEXT,
                created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                exported_at              TIMESTAMP,
                built_at                 TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_v3_creator ON campaigns_v3(creator_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_v3_status ON campaigns_v3(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_v3_target ON campaigns_v3(target_type, target_value)")

        # ── product cache tables (from product_api.ArcherAPI._init_cache) ─
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                asin TEXT PRIMARY KEY,
                brand_id TEXT,
                company_name TEXT,
                product_name TEXT,
                price TEXT,
                commission_payout TEXT,
                product_category TEXT,
                sub_category TEXT,
                avg_rating TEXT,
                total_reviews TEXT,
                image_encoded_string TEXT,
                deal_json TEXT,
                product_status TEXT,
                steph_revenue REAL,
                steph_units INTEGER,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS click_log (
                id            {_PK},
                asin          TEXT,
                slug          TEXT,
                fbclid        TEXT,
                attribution_url TEXT,
                clicked_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                slug TEXT PRIMARY KEY,
                campaign_type TEXT DEFAULT 'organic',
                routing TEXT DEFAULT 'landing',
                products_json TEXT,
                variants_json TEXT,
                spend_budget REAL DEFAULT 0,
                forecast_roas TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        init_collection_content_drafts_schema(conn)
        init_walmart_trends_schema(conn)
        init_amazon_trends_schema(conn)

        conn.commit()
    finally:
        conn.close()


def init_collection_content_drafts_schema(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS collection_content_drafts (
            id {_PK},
            source_type TEXT NOT NULL,
            source_collection_slug TEXT NOT NULL,
            source_collection_id TEXT,
            creator_id TEXT DEFAULT 'everydaywithsteph',
            title TEXT,
            description TEXT,
            voice_source_text TEXT,
            voice_raw_transcript TEXT,
            cleaned_transcript TEXT,
            social_post TEXT,
            landing_intro TEXT,
            hooks_json TEXT,
            cta TEXT,
            platform TEXT DEFAULT 'facebook_group',
            tone TEXT,
            product_snapshot_json TEXT,
            status TEXT DEFAULT 'draft',
            public_slug TEXT,
            published_collage_slug TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            published_at TEXT
        )
    """)
    _add_column_if_missing(conn, 'collection_content_drafts', "cleaned_transcript TEXT")
    _add_column_if_missing(conn, 'collection_content_drafts', "theme TEXT DEFAULT 'peach'")
    _add_column_if_missing(conn, 'collection_content_drafts', "layout TEXT DEFAULT 'layout-2'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_content_source "
        "ON collection_content_drafts(source_type, source_collection_slug)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_content_creator "
        "ON collection_content_drafts(creator_id, status)"
    )


def init_walmart_trends_schema(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS walmart_refresh_runs (
            id {_PK},
            source_type TEXT NOT NULL,
            source_file TEXT,
            window_start DATE,
            window_end DATE,
            status TEXT NOT NULL DEFAULT 'running',
            counts_json TEXT DEFAULT '{{}}',
            failures_json TEXT DEFAULT '[]',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_runs_status ON walmart_refresh_runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_runs_source ON walmart_refresh_runs(source_type, window_end)")
    _add_column_if_missing(conn, 'walmart_refresh_runs', "date_label TEXT")
    _add_column_if_missing(conn, 'walmart_refresh_runs', "run_metadata_json TEXT DEFAULT '{}'")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS walmart_products (
            sku TEXT PRIMARY KEY,
            item_name TEXT,
            product_title TEXT,
            brand TEXT,
            category_list TEXT,
            taxonomy TEXT,
            image_url TEXT,
            current_price REAL,
            price_display TEXT,
            availability TEXT,
            rating REAL,
            review_count INTEGER,
            canonical_url TEXT,
            enrichment_status TEXT DEFAULT 'pending',
            enrichment_error TEXT,
            raw_product_json TEXT DEFAULT '{}',
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_products_category ON walmart_products(category_list)")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS walmart_product_performance_snapshots (
            id {_PK},
            refresh_run_id INTEGER,
            sku TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_list_type TEXT,
            collection_name TEXT,
            window_start DATE,
            window_end DATE,
            item_count INTEGER DEFAULT 0,
            sale_amount REAL DEFAULT 0,
            total_earnings REAL DEFAULT 0,
            rank INTEGER,
            metadata_json TEXT DEFAULT '{{}}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(refresh_run_id, sku, source_list_type, collection_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_perf_sku ON walmart_product_performance_snapshots(sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_perf_run ON walmart_product_performance_snapshots(refresh_run_id)")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS walmart_affiliate_links (
            id {_PK},
            sku TEXT NOT NULL,
            product_url TEXT NOT NULL,
            impact_url TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sku, product_url)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_affiliate_sku ON walmart_affiliate_links(sku)")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS walmart_urlgenius_links (
            id {_PK},
            destination_url TEXT NOT NULL UNIQUE,
            genius_url TEXT NOT NULL,
            link_id TEXT,
            status TEXT DEFAULT 'active',
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS walmart_collections (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            source_type TEXT NOT NULL,
            refresh_run_id INTEGER,
            display_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            metadata_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_collections_active ON walmart_collections(is_active, display_order)")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS walmart_collection_items (
            id {_PK},
            collection_slug TEXT NOT NULL,
            sku TEXT NOT NULL,
            refresh_run_id INTEGER,
            display_order INTEGER DEFAULT 0,
            item_count INTEGER DEFAULT 0,
            sale_amount REAL DEFAULT 0,
            total_earnings REAL DEFAULT 0,
            badges_json TEXT DEFAULT '[]',
            metadata_json TEXT DEFAULT '{{}}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(collection_slug, sku)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_collection_items_slug ON walmart_collection_items(collection_slug, display_order)")

    _add_column_if_missing(conn, 'walmart_collections', "retailer TEXT DEFAULT 'walmart'")
    _add_column_if_missing(conn, 'walmart_collection_items', "retailer TEXT DEFAULT 'walmart'")


def init_amazon_trends_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS amazon_trend_products (
            asin                TEXT PRIMARY KEY,
            product_title       TEXT,
            brand               TEXT,
            category            TEXT,
            amazon_link         TEXT,
            image_url           TEXT,
            current_price       REAL,
            price_display       TEXT,
            enrichment_status   TEXT DEFAULT 'pending',
            first_seen_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_amazon_products_category ON amazon_trend_products(category)")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS amazon_product_performance_snapshots (
            id                  {_PK},
            refresh_run_id      INTEGER,
            asin                TEXT NOT NULL,
            source_list_type    TEXT,
            collection_name     TEXT,
            clicks              INTEGER DEFAULT 0,
            items_ordered       INTEGER DEFAULT 0,
            items_shipped       INTEGER DEFAULT 0,
            items_returned      INTEGER DEFAULT 0,
            total_earnings      REAL DEFAULT 0,
            rank                INTEGER,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(refresh_run_id, asin, source_list_type, collection_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_amazon_perf_asin ON amazon_product_performance_snapshots(asin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_amazon_perf_run  ON amazon_product_performance_snapshots(refresh_run_id)")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS amazon_affiliate_links (
            id              {_PK},
            asin            TEXT NOT NULL UNIQUE,
            product_url     TEXT NOT NULL,
            affiliate_url   TEXT NOT NULL,
            status          TEXT DEFAULT 'workbook',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_amazon_affiliate_asin ON amazon_affiliate_links(asin)")

    _add_column_if_missing(conn, 'amazon_trend_products', "enrichment_error TEXT")
    _add_column_if_missing(conn, 'amazon_trend_products', "last_verified_at TIMESTAMP")
    _add_column_if_missing(conn, 'amazon_trend_products', "availability_type TEXT")
    _add_column_if_missing(conn, 'amazon_trend_products', "availability_message TEXT")
    _add_column_if_missing(conn, 'amazon_trend_products', "parent_asin TEXT")
    _add_column_if_missing(conn, 'amazon_trend_products', "detail_page_url TEXT")


# ─────────────────────────────────────────────────────────────────────────────
# Creator helpers
# ─────────────────────────────────────────────────────────────────────────────

def seed_default_creator() -> None:
    """Insert the default Steph row if creators is empty. Idempotent."""
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) as n FROM creators").fetchone()
        n = count['n'] if hasattr(count, 'get') else count[0]
        if n == 0:
            cols = list(DEFAULT_CREATOR.keys())
            placeholders = ', '.join('?' for _ in cols)
            conn.execute(
                f"INSERT INTO creators ({', '.join(cols)}) VALUES ({placeholders})",
                [DEFAULT_CREATOR[c] for c in cols],
            )
            conn.commit()
            logging.info("[DB_SCHEMA] Seeded default creator: everydaywithsteph")
    finally:
        conn.close()


def get_creator(creator_id: str = 'everydaywithsteph') -> dict:
    """Fetch a creator row as a dict. Falls back to DEFAULT_CREATOR if missing."""
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM creators WHERE id = ?", (creator_id,)
            ).fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"[DB_SCHEMA] get_creator fallback (table missing): {e}")
    return dict(DEFAULT_CREATOR)


def list_creators() -> list[dict]:
    """Return all creators, ordered by display_name."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM creators ORDER BY display_name"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"[DB_SCHEMA] list_creators fallback (table missing): {e}")
        return [dict(DEFAULT_CREATOR)]


def upsert_creator(creator: dict) -> dict:
    """Insert or update a creator. Returns the saved row."""
    if not creator.get('id'):
        raise ValueError("creator.id is required")
    conn = _connect()
    try:
        cols = [
            'id', 'display_name', 'handle', 'brand_label', 'fb_pixel_id',
            'fb_page_id', 'amazon_tag', 'meta_ad_account_id', 'ltk_url',
            'facebook_url', 'voice_prompt', 'theme_default', 'defaults_json',
        ]
        values = [creator.get(c) for c in cols]
        placeholders = ', '.join('?' for _ in cols)
        update_set = ', '.join(
            f"{c} = EXCLUDED.{c}" for c in cols if c != 'id'
        )
        conn.execute(
            f"INSERT INTO creators ({', '.join(cols)}, updated_at) "
            f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
            f"ON CONFLICT (id) DO UPDATE SET {update_set}, updated_at = CURRENT_TIMESTAMP",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return get_creator(creator['id'])


def add_campaign_type_to_collage(slug: str, campaign_type: str) -> None:
    """Append 'organic' or 'paid' to a collage's campaign_types JSON array (dedup)."""
    if campaign_type not in ('organic', 'paid'):
        return
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT campaign_types FROM collages WHERE slug = ?", (slug,)
        ).fetchone()
        if not row:
            return
        raw = row['campaign_types'] if hasattr(row, 'get') else row[0]
        try:
            current = json.loads(raw or '[]')
            if not isinstance(current, list):
                current = []
        except (json.JSONDecodeError, TypeError):
            current = []
        if campaign_type not in current:
            current.append(campaign_type)
            conn.execute(
                "UPDATE collages SET campaign_types = ? WHERE slug = ?",
                (json.dumps(current), slug),
            )
            conn.commit()
    finally:
        conn.close()


_SQLITE_SEED_TABLES: list[tuple[str, str | list]] = [
    ("creators",                  "id"),
    ("collages",                  "slug"),
    ("posts",                     "id"),
    ("earnings_amazon",           "id"),
    ("attribution_paid",          "id"),
    ("storefront_chat_sessions",  "id"),
    ("campaigns_v3",              "id"),
    ("products",                  "asin"),
    ("cache_meta",                "key"),
    ("click_log",                 "id"),
    ("campaigns",                 "slug"),
    ("collection_content_drafts", "id"),
    ("walmart_refresh_runs",      "id"),
    ("walmart_products",          "sku"),
    ("walmart_product_performance_snapshots", ["refresh_run_id", "sku", "source_list_type", "collection_name"]),
    ("walmart_affiliate_links",   ["sku", "product_url"]),
    ("walmart_urlgenius_links",   "destination_url"),
    ("walmart_collections",       "slug"),
    ("walmart_collection_items",  ["collection_slug", "sku"]),
    ("amazon_trend_products",     "asin"),
    ("amazon_product_performance_snapshots", ["refresh_run_id", "asin", "source_list_type", "collection_name"]),
    ("amazon_affiliate_links",    "asin"),
]

_SERIAL_TABLES = [
    "posts", "earnings_amazon", "attribution_paid", "storefront_chat_sessions",
    "campaigns_v3", "click_log", "collection_content_drafts", "walmart_refresh_runs",
    "walmart_product_performance_snapshots", "walmart_affiliate_links",
    "walmart_urlgenius_links", "walmart_collection_items",
    "amazon_product_performance_snapshots", "amazon_affiliate_links",
]


def _seed_from_sqlite_snapshot(sqlite_path: str) -> None:
    """
    Copy rows from a SQLite snapshot into the PostgreSQL database.
    Uses ON CONFLICT DO NOTHING so it is safe to call multiple times.
    Only runs when PostgreSQL is active and walmart_products is empty.
    """
    import sqlite3 as _sqlite3

    if not _USE_PG:
        return
    if not os.path.exists(sqlite_path):
        logging.info("[DB_SEED] SQLite snapshot not found at %s — skipping seed.", sqlite_path)
        return

    pg = _connect()
    try:
        row = pg.execute("SELECT COUNT(*) as n FROM walmart_products").fetchone()
        count = row['n'] if row else 0
    except Exception:
        count = 0
    finally:
        pg.close()

    if count > 0:
        logging.info("[DB_SEED] walmart_products already has %d rows — skipping seed.", count)
        return

    logging.info("[DB_SEED] Seeding PostgreSQL from SQLite snapshot %s …", sqlite_path)

    sc = _sqlite3.connect(sqlite_path, timeout=30)
    sc.row_factory = _sqlite3.Row
    pc = _connect()

    total = 0
    try:
        for table, pk in _SQLITE_SEED_TABLES:
            sl_exists = sc.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not sl_exists:
                continue

            rows = sc.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue

            sl_cols = list(rows[0].keys())

            pg_cur = pc._conn.cursor()
            pg_cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s", (table,)
            )
            pg_cols = {r[0] for r in pg_cur.fetchall()}
            cols = [c for c in sl_cols if c in pg_cols]
            if not cols:
                continue

            pks = [pk] if isinstance(pk, str) else pk
            conflict = ", ".join(f'"{c}"' for c in pks)
            col_str  = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join(["%s"] * len(cols))
            sql = (
                f'INSERT INTO "{table}" ({col_str}) VALUES ({placeholders}) '
                f"ON CONFLICT ({conflict}) DO NOTHING"
            )

            inserted = 0
            cur = pc._conn.cursor()
            for row in rows:
                vals = []
                for c in cols:
                    v = row[c]
                    if isinstance(v, bytes):
                        v = v.decode("utf-8", errors="replace")
                    vals.append(v)
                try:
                    cur.execute("SAVEPOINT sp")
                    cur.execute(sql, vals)
                    if cur.rowcount:
                        inserted += 1
                    cur.execute("RELEASE SAVEPOINT sp")
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp")
                    cur.execute("RELEASE SAVEPOINT sp")
                    logging.debug("[DB_SEED] %s row skipped: %s", table, e)

            pc._conn.commit()
            total += inserted
            logging.info("[DB_SEED]   %s: %d rows inserted", table, inserted)

        logging.info("[DB_SEED] Resetting sequences …")
        seq_cur = pc._conn.cursor()
        for table in _SERIAL_TABLES:
            try:
                seq_cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}','id'),"
                    f"COALESCE(MAX(id),1),true) FROM \"{table}\""
                )
                pc._conn.commit()
            except Exception as e:
                pc._conn.rollback()
                logging.debug("[DB_SEED] sequence reset skipped for %s: %s", table, e)

    finally:
        sc.close()
        pc.close()

    logging.info("[DB_SEED] Seed complete. Total rows inserted: %d", total)


_seed_thread_started = False


def bootstrap() -> None:
    """One-shot initialiser called at app boot.

    init_schema + seed_default_creator run synchronously (fast DDL + 1 row).
    _seed_from_sqlite_snapshot runs in a daemon thread so gunicorn workers
    become ready immediately and pass Cloud Run health checks while the
    bulk import proceeds in the background.

    Safe to call multiple times — the seed thread is guarded by a per-process
    flag so it starts at most once regardless of how many times bootstrap() is
    invoked (e.g. from walmart_trends, amazon_trends, etc.).
    """
    global _seed_thread_started
    init_schema()
    seed_default_creator()
    if _USE_PG and os.path.exists(DB_PATH) and not _seed_thread_started:
        _seed_thread_started = True
        import threading
        t = threading.Thread(
            target=_seed_from_sqlite_snapshot,
            args=(DB_PATH,),
            daemon=True,
            name="sqlite-seed",
        )
        t.start()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    bootstrap()
    print(f"[DB_SCHEMA] Bootstrap complete. Creators: {len(list_creators())}")
