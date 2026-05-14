#!/usr/bin/env python3
"""Clean contaminated Walmart product brand values."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db_schema
from walmart_trends import normalize_product_brand


def cleanup_walmart_brands(apply: bool = False) -> dict[str, int]:
    db_schema.bootstrap()
    conn = sqlite3.connect(db_schema.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT sku, brand, product_title, item_name
            FROM walmart_products
            WHERE COALESCE(brand, '') != ''
            """
        ).fetchall()
        checked = len(rows)
        changed = 0
        inferred = 0
        cleared = 0
        for row in rows:
            title = row["product_title"] or row["item_name"] or ""
            current = row["brand"] or ""
            normalized = normalize_product_brand(current, title)
            if normalized == current:
                continue
            changed += 1
            if normalized:
                inferred += 1
            else:
                cleared += 1
            if apply:
                conn.execute(
                    "UPDATE walmart_products SET brand = ?, updated_at = CURRENT_TIMESTAMP WHERE sku = ?",
                    (normalized, row["sku"]),
                )
        if apply:
            conn.commit()
        return {
            "checked": checked,
            "changed": changed,
            "inferred": inferred,
            "cleared": cleared,
            "applied": int(apply),
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean contaminated Walmart product brands")
    parser.add_argument("--apply", action="store_true", help="write updates; without this, only report counts")
    args = parser.parse_args()
    result = cleanup_walmart_brands(apply=args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(
        f"{mode}: checked={result['checked']} changed={result['changed']} "
        f"inferred={result['inferred']} cleared={result['cleared']}"
    )


if __name__ == "__main__":
    main()
