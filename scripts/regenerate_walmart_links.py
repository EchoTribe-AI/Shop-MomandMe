#!/usr/bin/env python3
"""Inspect/regenerate stale Walmart Impact + URLGenius links.

Examples:
  uv run python scripts/regenerate_walmart_links.py inspect --sku 5454929532
  uv run python scripts/regenerate_walmart_links.py regenerate --sku 5454929532
  uv run python scripts/regenerate_walmart_links.py regenerate-stale --limit 25
  uv run python scripts/regenerate_walmart_links.py rebuild-all --dry-run
  uv run python scripts/regenerate_walmart_links.py rebuild-all --limit 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db_schema
from walmart_trends import WalmartLinkRegenerationService


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect/regenerate stale Walmart URLGenius links")
    parser.add_argument(
        "--db-path",
        help="Optional SQLite DB path. Defaults to CACHE_DB_PATH/db_schema.DB_PATH.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspect one Walmart SKU")
    inspect.add_argument("--sku", required=True)
    inspect.add_argument("--include-redirect", action="store_true", help="Check URLGenius first-hop redirect")

    regen = sub.add_parser("regenerate", help="Regenerate one stale Walmart SKU")
    regen.add_argument("--sku", required=True)
    regen.add_argument("--force", action="store_true", help="Regenerate even when local stale detection is false")
    regen.add_argument("--include-redirect", action="store_true", help="Check URLGenius first-hop redirect before deciding")

    bulk = sub.add_parser("regenerate-stale", help="Regenerate all locally detected stale Walmart SKUs")
    bulk.add_argument("--limit", type=int)
    bulk.add_argument("--include-redirect", action="store_true", help="Check first-hop redirects while regenerating candidates")

    rebuild = sub.add_parser("rebuild-all", help="Force rebuild every current Walmart SKU affiliate + URLGenius link")
    rebuild.add_argument("--limit", type=int, help="Only rebuild the first N discovered SKUs")
    rebuild.add_argument("--dry-run", action="store_true", help="Report what would be rebuilt without writing changes")

    args = parser.parse_args()
    if args.db_path:
        os.environ["CACHE_DB_PATH"] = args.db_path
        db_schema.DB_PATH = args.db_path
        import walmart_trends

        walmart_trends.DB_PATH = args.db_path

    db_schema.bootstrap()
    service = WalmartLinkRegenerationService()
    if args.command == "inspect":
        _print_json(service.inspect_sku(args.sku, include_redirect=args.include_redirect))
    elif args.command == "regenerate":
        _print_json(service.regenerate_sku(args.sku, force=args.force, include_redirect=args.include_redirect))
    elif args.command == "regenerate-stale":
        _print_json(service.regenerate_all_stale(limit=args.limit, include_redirect=args.include_redirect))
    elif args.command == "rebuild-all":
        _print_json(service.rebuild_all(limit=args.limit, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
