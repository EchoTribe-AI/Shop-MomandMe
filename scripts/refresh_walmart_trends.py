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
        "--link-mode",
        choices=["urlgenius", "workbook-only"],
        default=None,
        help="Bootstrap link mode. urlgenius creates URLGenius links directly from Walmart URLs; workbook-only uses canonical Walmart URLs.",
    )
    parser.add_argument(
        "--skip-links",
        action="store_true",
        default=None,
        help="Backward-compatible alias for --link-mode workbook-only in bootstrap mode.",
    )
    parser.add_argument(
        "--with-links",
        action="store_true",
        default=None,
        help="Backward-compatible alias for --link-mode urlgenius in bootstrap mode. This does not enable Impact links.",
    )
    args = parser.parse_args()
    link_mode = args.link_mode
    if args.skip_links:
        link_mode = "workbook-only"
    elif args.with_links:
        link_mode = "urlgenius"
    if link_mode is None:
        link_mode = "urlgenius" if args.mode == "bootstrap" else "impact-urlgenius"

    service = WalmartTrendRefreshService()
    try:
        if args.mode == "bootstrap":
            result = service.bootstrap_from_workbook(args.workbook, link_mode=link_mode)
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
