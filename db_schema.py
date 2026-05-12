"""
Multi-creator + insights schema migrations.

Runs idempotent CREATE TABLE / ADD COLUMN statements against the existing
sqlite database (data/archer_catalog.db). Safe to call on every app boot.

Tables introduced
-----------------
creators          — per-creator brand/voice/auth config (Steph seeded by default)
earnings_amazon   — Amazon Associates earnings rows from manual CSV uploads
attribution_paid  — Archer (and later Impact) paid-ad attribution snapshots
storefront_chat_sessions — lightweight creator-scoped public shop chat memory

Columns added to collages
-------------------------
creator_id      — FK-by-convention to creators.id (default 'everydaywithsteph')
status          — 'draft' | 'published' (existing rows backfilled to 'published')
campaign_types  — JSON array of {'organic','paid'} signaling where the
                  collection has been used (auto-tagged on Mode C save / Ad
                  Builder use)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3

DB_PATH = os.environ.get('CACHE_DB_PATH', 'data/archer_catalog.db')

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
    # Per-creator ad defaults (Q7 — overrides hardcoded spec defaults).
    # Stored as JSON in creators.defaults_json. Empty {} = use spec defaults.
    'defaults_json':      json.dumps({}),
}


def _connect(timeout: int = 30) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col_def: str) -> None:
    """Run ALTER TABLE … ADD COLUMN, swallowing 'duplicate column name' errors."""
    col_name = col_def.split()[0]
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if col_name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")


def init_schema() -> None:
    """Create new tables and patch collages columns. Idempotent."""
    conn = _connect()
    try:
        # ── creators ──────────────────────────────────────────────────────
        conn.execute("""
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
        # Branch 3 columns added via ALTER (idempotent for existing DBs)
        _add_column_if_missing(conn, 'creators', "fb_page_id TEXT")
        _add_column_if_missing(conn, 'creators', "defaults_json TEXT DEFAULT '{}'")

        # ── earnings_amazon (manual CSV uploads) ─────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS earnings_amazon (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
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

        # ── attribution_paid (Archer + future Impact pulls) ──────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attribution_paid (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id   TEXT NOT NULL,
                network      TEXT NOT NULL,    -- 'archer' | 'impact'
                label        TEXT,             -- maps to utm_campaign / layer label
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

        # ── collages: public collection landing pages ───────────────────
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
        # ── collages: ADD COLUMN backfills ───────────────────────────────
        _add_column_if_missing(conn, 'collages', "creator_id TEXT DEFAULT 'everydaywithsteph'")
        _add_column_if_missing(conn, 'collages', "status TEXT DEFAULT 'published'")
        _add_column_if_missing(conn, 'collages', "campaign_types TEXT DEFAULT '[\"organic\"]'")
        _add_column_if_missing(conn, 'collages', "hero_title TEXT")
        _add_column_if_missing(conn, 'collages', "hero_subtitle TEXT")

        # ── posts (Branch 2B) ────────────────────────────────────────────
        # Persists Mode B individual social posts (1 per product). Optional
        # foreign key to a collection slug so a "Mother's Day" gift guide
        # can have N individual posts AND one collection landing page, all
        # queryable together.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
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

        # Public storefront chat memory. The browser owns the opaque
        # session_id; the server keeps only the last few turns per creator.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storefront_chat_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
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

        # ── campaigns_v3 (Branch 3) ──────────────────────────────────────
        # Persists Campaign Build Packages per the Campaign_Build_Package_Spec.
        # Each row is one buildable package (one ASIN OR one collection OR one
        # boosted post). Bulk generation creates N rows. The full spec-compliant
        # JSON lives in package_json so the export step is just a SELECT.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns_v3 (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id               TEXT NOT NULL DEFAULT 'everydaywithsteph',
                package_type             TEXT NOT NULL,        -- 'new_campaign' | 'boost_post'
                target_type              TEXT NOT NULL,        -- 'asin' | 'collection' | 'post'
                target_value             TEXT NOT NULL,        -- ASIN, slug, or post id
                brand_slug               TEXT,
                product_slug             TEXT,
                product_name             TEXT,
                destination_url          TEXT,
                layers_json              TEXT,                 -- ['L1','L2','L3'] selected
                asset_url                TEXT,                 -- shared image/video URL
                asset_type               TEXT DEFAULT 'static_image',
                package_json             TEXT NOT NULL,        -- full spec-compliant JSON
                defaults_overrides_json  TEXT,                 -- user tweaks
                utm_auto                 INTEGER DEFAULT 1,
                status                   TEXT DEFAULT 'draft', -- draft | exported | built
                meta_campaign_ids_json   TEXT,
                meta_post_id             TEXT,                 -- for boost_post packages
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

        init_collection_content_drafts_schema(conn)
        init_walmart_trends_schema(conn)
        init_amazon_trends_schema(conn)

        conn.commit()
    finally:
        conn.close()


def init_collection_content_drafts_schema(conn: sqlite3.Connection) -> None:
    """Create draft content table for trend collection post/page generation."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_content_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_content_source "
        "ON collection_content_drafts(source_type, source_collection_slug)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_content_creator "
        "ON collection_content_drafts(creator_id, status)"
    )


def init_walmart_trends_schema(conn: sqlite3.Connection) -> None:
    """Create normalized tables for the Walmart What's Trending Now workflow."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS walmart_refresh_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_file TEXT,
            window_start DATE,
            window_end DATE,
            status TEXT NOT NULL DEFAULT 'running',
            counts_json TEXT DEFAULT '{}',
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS walmart_product_performance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            metadata_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(refresh_run_id, sku, source_list_type, collection_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_perf_sku ON walmart_product_performance_snapshots(sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_perf_run ON walmart_product_performance_snapshots(refresh_run_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS walmart_affiliate_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS walmart_urlgenius_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS walmart_collection_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_slug TEXT NOT NULL,
            sku TEXT NOT NULL,
            refresh_run_id INTEGER,
            display_order INTEGER DEFAULT 0,
            item_count INTEGER DEFAULT 0,
            sale_amount REAL DEFAULT 0,
            total_earnings REAL DEFAULT 0,
            badges_json TEXT DEFAULT '[]',
            metadata_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(collection_slug, sku)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walmart_collection_items_slug ON walmart_collection_items(collection_slug, display_order)")

    # retailer = 'walmart' | 'amazon' — identifies product source for render/join logic.
    # source_type on these tables records the ingest method, not the retailer.
    _add_column_if_missing(conn, 'walmart_collections', "retailer TEXT DEFAULT 'walmart'")
    _add_column_if_missing(conn, 'walmart_collection_items', "retailer TEXT DEFAULT 'walmart'")


def init_amazon_trends_schema(conn: sqlite3.Connection) -> None:
    """Create normalized tables for Amazon trend ingestion.

    Kept separate from Walmart tables per architecture requirement.
    Collections are stored in the shared walmart_collections / walmart_collection_items
    tables (long-term rename target: trend_collections / trend_collection_items)
    with retailer='amazon' discriminator. Amazon products are NOT stored in walmart_products.
    """
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS amazon_product_performance_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS amazon_affiliate_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            asin            TEXT NOT NULL UNIQUE,
            product_url     TEXT NOT NULL,
            affiliate_url   TEXT NOT NULL,
            status          TEXT DEFAULT 'workbook',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_amazon_affiliate_asin ON amazon_affiliate_links(asin)")


def seed_default_creator() -> None:
    """Insert the default Steph row if creators is empty. Idempotent."""
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
        if count == 0:
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
    """Fetch a creator row as a dict. Falls back to DEFAULT_CREATOR if the row
    is missing OR if the table itself doesn't exist yet (e.g. during a boot
    race before bootstrap() has run)."""
    try:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM creators WHERE id = ?", (creator_id,)
            ).fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        # Table doesn't exist yet — safe fallback during boot
        logging.warning(f"[DB_SCHEMA] get_creator fallback (table missing): {e}")
    return dict(DEFAULT_CREATOR)


def list_creators() -> list[dict]:
    """Return all creators, ordered by display_name. Defensive: returns
    [DEFAULT_CREATOR] if the table doesn't exist yet."""
    try:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM creators ORDER BY display_name"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        logging.warning(f"[DB_SCHEMA] list_creators fallback (table missing): {e}")
        return [dict(DEFAULT_CREATOR)]


def upsert_creator(creator: dict) -> dict:
    """Insert or update a creator. Returns the saved row."""
    if not creator.get('id'):
        raise ValueError("creator.id is required")
    conn = _connect()
    try:
        # Build the canonical column set so we never silently drop fields
        cols = [
            'id', 'display_name', 'handle', 'brand_label', 'fb_pixel_id',
            'fb_page_id', 'amazon_tag', 'meta_ad_account_id', 'ltk_url',
            'facebook_url', 'voice_prompt', 'theme_default', 'defaults_json',
        ]
        values = [creator.get(c) for c in cols]
        placeholders = ', '.join('?' for _ in cols)
        conn.execute(
            f"INSERT OR REPLACE INTO creators ({', '.join(cols)}, updated_at) "
            f"VALUES ({placeholders}, CURRENT_TIMESTAMP)",
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
        try:
            current = json.loads(row[0] or '[]')
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


def bootstrap() -> None:
    """One-shot initializer called at app boot."""
    init_schema()
    seed_default_creator()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    bootstrap()
    print(f"[DB_SCHEMA] Bootstrap complete. Creators: {len(list_creators())}")
