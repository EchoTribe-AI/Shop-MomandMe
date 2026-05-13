"""Validate Amazon Creators API hydration end-to-end.

Steps:
  1. Report which env vars are present (presence only, not values).
  2. Detect v2.x vs v3.x credential family.
  3. Fetch one access token.
  4. Call GetItems with a small ASIN batch.
  5. Persist into amazon_trend_products and show before/after for one ASIN.

Stops at the first hard failure with a clear message so we know exactly what's
missing/broken. Intended for local/manual use; safe to run repeatedly.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Make sibling modules importable when run as `python scripts/validate_amazon_creators.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv_if_present() -> None:
    """Best-effort load of nearby .env files (no external dependency)."""
    candidates = [
        ROOT / ".env",
        Path("/Users/kellmaster/Documents/Claude/Projects/EchoTribe Dashboard/.env"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--asins", nargs="+", default=["B09B2SBHQK", "B09B8V1LZ3"],
        help="ASINs to hydrate (default: docs sample pair)",
    )
    parser.add_argument(
        "--persist", action="store_true",
        help="Also update amazon_trend_products and print before/after",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _load_dotenv_if_present()

    from utils.amazon_creators import (
        AmazonCreatorsAPI,
        AmazonCreatorsAPIError,
        AmazonCreatorsConfigError,
        load_config,
    )

    # Step 1 — env presence
    cfg = load_config()
    print("=== Step 1: Config / env vars ===")
    for k, v in cfg.items():
        if k in ("client_id", "client_secret"):
            print(f"  {k}: {'SET' if v else 'MISSING'}")
        else:
            print(f"  {k}: {v or 'MISSING'}")
    print("  (sources: AMAZON_CREATORS_CLIENT_ID|CREDENTIAL_ID, "
          "AMAZON_CREATORS_CLIENT_SECRET|CREDENTIAL_SECRET, "
          "AMAZON_CREATORS_CREDENTIAL_VERSION [default 3.1], "
          "AMAZON_PARTNER_TAG|AMAZON_AFFILIATE_TAG, "
          "AMAZON_MARKETPLACE [default www.amazon.com])")

    # Step 2 — version detection
    try:
        client = AmazonCreatorsAPI(cfg)
    except AmazonCreatorsConfigError as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"\n=== Step 2: Credential family ===")
    print(f"  version={client.version}  family={client.family}")
    missing = client.missing_config()
    if missing:
        print(f"FAIL: missing config: {', '.join(missing)}")
        return 2

    # Step 3 — token fetch
    print("\n=== Step 3: Access token ===")
    try:
        token = client.access_token()
    except (AmazonCreatorsAPIError, AmazonCreatorsConfigError) as exc:
        print(f"FAIL: token fetch — {exc}")
        return 3
    print(f"  acquired token (length={len(token)}, family={client.family}) — OK")

    # Step 4 — GetItems
    print("\n=== Step 4: GetItems ===")
    try:
        items = client.get_items(args.asins)
    except (AmazonCreatorsAPIError, AmazonCreatorsConfigError) as exc:
        print(f"FAIL: GetItems — {exc}")
        return 4
    print(f"  requested={len(args.asins)}  returned={len(items)}")
    for asin in args.asins:
        item = items.get(asin)
        if not item:
            print(f"  - {asin}: NO DATA")
            continue
        print(f"  - {asin}: title={item.get('product_title','')[:60]!r}")
        print(f"      image_url       = {item.get('image_url') or 'EMPTY'}")
        print(f"      price_display   = {item.get('price_display') or 'EMPTY'}")
        print(f"      availability    = {item.get('availability_type') or 'EMPTY'}")
        print(f"      parent_asin     = {item.get('parent_asin') or 'EMPTY'}")
        print(f"      detail_page_url = {item.get('detail_page_url') or 'EMPTY'}")

    # Step 5 — persist
    if args.persist:
        print("\n=== Step 5: persist + before/after ===")
        import db_schema
        from amazon_trends import AmazonTrendStore

        db_schema.bootstrap()
        store = AmazonTrendStore()
        target = args.asins[0]
        # Insert a stub row if missing so update_product_enrichment has a target.
        store.upsert_product(__stub(target))
        before = store.get_product(target) or {}
        parsed = items.get(target) or {}
        if parsed:
            store.update_product_enrichment(target, parsed, "ok")
        after = store.get_product(target) or {}
        cols = ("asin", "product_title", "image_url", "price_display",
                "availability_type", "parent_asin", "detail_page_url",
                "enrichment_status", "last_verified_at")
        print(f"  BEFORE: {json.dumps({c: before.get(c) for c in cols}, default=str, indent=2)}")
        print(f"  AFTER : {json.dumps({c: after.get(c) for c in cols}, default=str, indent=2)}")

    print("\nOK")
    return 0


def __stub(asin: str):
    from amazon_trends import AmazonTrendRecord
    return AmazonTrendRecord(asin=asin)


if __name__ == "__main__":
    raise SystemExit(main())
