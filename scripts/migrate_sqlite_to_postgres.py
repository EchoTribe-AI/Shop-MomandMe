#!/usr/bin/env python3
"""
One-shot migration: copy all data from SQLite → PostgreSQL.

Run once after provisioning PostgreSQL:
    python3 scripts/migrate_sqlite_to_postgres.py

Safe to re-run: every table uses ON CONFLICT DO NOTHING so existing rows are
skipped rather than overwritten.
"""
from __future__ import annotations

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Deps check ────────────────────────────────────────────────────────────────
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    logging.error("psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    logging.error("DATABASE_URL not set — cannot connect to PostgreSQL.")
    sys.exit(1)

SQLITE_PATH = os.environ.get("CACHE_DB_PATH", "data/archer_catalog.db")
if not os.path.exists(SQLITE_PATH):
    logging.warning(f"SQLite DB not found at {SQLITE_PATH} — nothing to migrate.")
    sys.exit(0)


import sqlite3

def sqlite_conn():
    conn = sqlite3.connect(SQLITE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def pg_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def table_exists_sqlite(sc, table: str) -> bool:
    row = sc.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def table_exists_pg(pc, table: str) -> bool:
    cur = pc.cursor()
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return cur.fetchone() is not None


def migrate_table(sc, pc, table: str, pk: str | list[str], *, batch: int = 500) -> int:
    if not table_exists_sqlite(sc, table):
        logging.info(f"  SKIP  {table} (not in SQLite)")
        return 0
    if not table_exists_pg(pc, table):
        logging.warning(f"  SKIP  {table} (not in PostgreSQL — run bootstrap first)")
        return 0

    rows = sc.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        logging.info(f"  EMPTY {table}")
        return 0

    cols = list(rows[0].keys())

    # Filter out columns that don't exist in PostgreSQL
    cur_pg = pc.cursor()
    cur_pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    pg_cols = {r[0] for r in cur_pg.fetchall()}
    cols = [c for c in cols if c in pg_cols]
    if not cols:
        logging.warning(f"  SKIP  {table} (no matching columns)")
        return 0

    placeholders = ", ".join(["%s"] * len(cols))
    col_str = ", ".join(f'"{c}"' for c in cols)

    if isinstance(pk, str):
        pk = [pk]
    conflict = ", ".join(f'"{c}"' for c in pk)

    sql = (
        f'INSERT INTO "{table}" ({col_str}) VALUES ({placeholders}) '
        f"ON CONFLICT ({conflict}) DO NOTHING"
    )

    inserted = 0
    cur = pc.cursor()
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        for row in chunk:
            values = []
            for c in cols:
                v = row[c]
                # Convert bytes to str; keep None as None
                if isinstance(v, bytes):
                    v = v.decode("utf-8", errors="replace")
                values.append(v)
            # Use a savepoint so a single bad row only rolls back itself,
            # not the entire batch that was already inserted.
            try:
                cur.execute("SAVEPOINT sp_row")
                cur.execute(sql, values)
                if cur.rowcount:
                    inserted += 1
                cur.execute("RELEASE SAVEPOINT sp_row")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_row")
                cur.execute("RELEASE SAVEPOINT sp_row")
                logging.warning(f"  WARN  {table} row skipped: {e}")
                continue
        pc.commit()
        logging.info(f"  {table}: batch {i//batch + 1} committed ({min(i+batch, len(rows))}/{len(rows)})")

    return inserted


# ── Bootstrap PostgreSQL schema first ─────────────────────────────────────────
logging.info("Bootstrapping PostgreSQL schema …")
import db_schema
db_schema.init_schema()
db_schema.seed_default_creator()
logging.info("Schema ready.")

# ── Migrate tables ─────────────────────────────────────────────────────────────
TABLES: list[tuple[str, str | list[str]]] = [
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

sc = sqlite_conn()
pc = pg_conn()
total = 0
for table, pk in TABLES:
    n = migrate_table(sc, pc, table, pk)
    if n:
        logging.info(f"  OK    {table}: {n} rows inserted")
    total += n

sc.close()

# ── Reset PostgreSQL SERIAL sequences to max(id) so next inserts don't collide ─
# Tables with SERIAL PRIMARY KEY on column 'id' need their sequences advanced
# after we bulk-inserted rows with explicit id values.
SERIAL_TABLES = [
    "posts",
    "earnings_amazon",
    "attribution_paid",
    "storefront_chat_sessions",
    "campaigns_v3",
    "click_log",
    "collection_content_drafts",
    "walmart_refresh_runs",
    "walmart_product_performance_snapshots",
    "walmart_affiliate_links",
    "walmart_urlgenius_links",
    "walmart_collection_items",
    "amazon_product_performance_snapshots",
    "amazon_affiliate_links",
]

logging.info("\nResetting PostgreSQL sequences …")
seq_cur = pc.cursor()
for table in SERIAL_TABLES:
    if not table_exists_pg(pc, table):
        continue
    try:
        seq_cur.execute(
            f"SELECT setval("
            f"  pg_get_serial_sequence('{table}', 'id'),"
            f"  COALESCE(MAX(id), 1),"
            f"  true"
            f") FROM \"{table}\""
        )
        row = seq_cur.fetchone()
        new_val = row[0] if row else '?'
        pc.commit()
        logging.info(f"  reset {table}.id sequence → {new_val}")
    except Exception as e:
        pc.rollback()
        logging.warning(f"  WARN  could not reset sequence for {table}: {e}")

seq_cur.close()
pc.close()

logging.info(f"\nMigration complete. Total rows inserted: {total}")
