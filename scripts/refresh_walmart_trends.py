#!/usr/bin/env python3
"""Manual/cron entrypoint for Walmart What's Trending Now refreshes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from walmart_trends import DEFAULT_WORKBOOK, RefreshAlreadyRunning, WalmartTrendRefreshService


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Walmart What's Trending Now data")
    parser.add_argument("--mode", choices=["bootstrap", "weekly"], default="weekly")
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK), help="Workbook path for bootstrap mode")
    parser.add_argument(
        "--skip-links",
        action="store_true",
        default=None,
        help="Skip Impact and URLGenius link generation. Defaults to true for bootstrap mode.",
    )
    parser.add_argument(
        "--with-links",
        action="store_false",
        dest="skip_links",
        help="Allow link generation. Do not use for Phase 1 workbook bootstrap.",
    )
    args = parser.parse_args()
    skip_links = args.skip_links if args.skip_links is not None else args.mode == "bootstrap"

    service = WalmartTrendRefreshService()
    try:
        if args.mode == "bootstrap":
            result = service.bootstrap_from_workbook(args.workbook, skip_link_generation=skip_links)
        else:
            result = service.refresh_from_impact()
    except RefreshAlreadyRunning as exc:
        print(json.dumps({"status": "locked", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps({
        "run_id": result.run_id,
        "status": result.status,
        "counts": result.counts,
        "failures": result.failures,
        "diagnostics": result.diagnostics,
    }, indent=2))
    return 0 if result.status in {"success", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
