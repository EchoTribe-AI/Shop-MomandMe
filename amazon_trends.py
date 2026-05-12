"""Amazon Associates trend ingestion workflow.

Handles Amazon workbook parsing, product storage, affiliate link management,
and collection publishing. Shares run-tracking, collections, and URLGenius
tables with the Walmart pipeline (retailer='amazon' scoping). Amazon product
data is stored exclusively in amazon_trend_products / amazon_affiliate_links.
"""
from __future__ import annotations

import json
import logging
import os
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import db_schema
from walmart_trends import (
    RefreshResult,
    WalmartTrendStore,
    WorkbookTrendParser,
    WorkbookValidationError,
    _connect,
    _price_display,
    _slugify,
    _to_float,
    _to_int,
    parse_workbook_filename,
)

AMAZON_AFFILIATE_TAG: str = os.environ.get("AMAZON_AFFILIATE_TAG", "mommymedeals-20")


def _ensure_amazon_tag(url: str, tag: str = "") -> str:
    """Append affiliate tag only when no 'tag' param is already present."""
    if not url:
        return url
    effective_tag = tag or AMAZON_AFFILIATE_TAG
    if not effective_tag:
        return url
    params = parse_qs(urlparse(url).query, keep_blank_values=True)
    if params.get("tag"):
        return url
    separator = "&" if urlparse(url).query else "?"
    return f"{url}{separator}tag={effective_tag}"


@dataclass
class AmazonTrendRecord:
    asin: str
    product_title: str = ""
    amazon_link: str = ""
    clicks: int = 0
    items_ordered: int = 0
    items_shipped: int = 0
    items_returned: int = 0
    items_shipped_revenue: float = 0.0
    items_shipped_earnings: float = 0.0
    total_earnings: float = 0.0
    collection_name: str = ""
    source_list_type: str = ""
    rank: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AmazonWorkbookParser(WorkbookTrendParser):
    """Parse an Amazon Associates trend workbook.

    Inherits all XLSX XML reading from WorkbookTrendParser. Overrides sheet
    definitions and record construction for Amazon column layout.

    Source list types:
        2A = Trending - Clicks First
        2B = Trending - Earnings First
        2C = Trending - Items Shipped First
    """

    REQUIRED_SHEETS = {
        "Trending - Clicks First": {"ASIN", "Product Title", "Clicks", "Total Earnings"},
        "Trending - Earnings First": {"ASIN", "Product Title", "Clicks", "Total Earnings"},
        "Trending - Items Shipped First": {"ASIN", "Product Title", "Items Shipped", "Total Earnings"},
        "Curated Collections": {"Collection", "ASIN", "Product Title"},
    }
    OPTIONAL_SHEETS = {
        "All Aggregated ASINs": {"ASIN"},
        "Summary": set(),
        "Category Click Summary": set(),
    }

    # Primary identifying column used to locate the real header row in each sheet.
    # Amazon workbooks prepend a title row + blank row before the column headers.
    _HEADER_KEY: dict[str, str] = {
        "Trending - Clicks First": "ASIN",
        "Trending - Earnings First": "ASIN",
        "Trending - Items Shipped First": "ASIN",
        "Curated Collections": "Collection",
        "All Aggregated ASINs": "ASIN",
        "Category Click Summary": "Category",
    }

    def _read_workbook_rows(self) -> dict[str, list[dict[str, str]]]:
        """Override: Amazon sheets have title → blank → headers → data.

        Scan each sheet for its header row by key column instead of assuming
        row 0 = headers. Summary stays positional (parent behaviour preserved).
        """
        with zipfile.ZipFile(self.workbook_path) as zf:
            shared = self._shared_strings(zf)
            sheet_paths = self._sheet_paths(zf)
            self.sheet_names_found = list(sheet_paths.keys())
            out: dict[str, list[dict[str, str]]] = {}
            for sheet_name, sheet_path in sheet_paths.items():
                raw_rows = self._sheet_rows(zf, sheet_path, shared)
                if not raw_rows:
                    out[sheet_name] = []
                    continue
                if sheet_name == "Summary":
                    # Positional — parent's _parse_summary_tab handles title/blank gracefully
                    rows = []
                    for raw in raw_rows:
                        if not any(raw):
                            continue
                        rows.append({f"col{i}": raw[i] for i in range(len(raw))})
                    out[sheet_name] = rows
                    continue
                key_col = self._HEADER_KEY.get(sheet_name, "")
                header_idx = self._find_header_row_index(raw_rows, key_col)
                headers = [h.strip() for h in raw_rows[header_idx]]
                rows = []
                for raw in raw_rows[header_idx + 1:]:
                    if not any(raw):
                        continue
                    rows.append({
                        headers[i]: raw[i] if i < len(raw) else ""
                        for i in range(len(headers))
                    })
                out[sheet_name] = rows
            return out

    def parse(self) -> dict[str, Any]:
        if not self.workbook_path.exists():
            raise FileNotFoundError(f"Workbook not found: {self.workbook_path}")

        rows_by_sheet = self._read_workbook_rows()
        self._validate(rows_by_sheet)
        return {
            "2A": self._amazon_records_from_sheet(
                rows_by_sheet.get("Trending - Clicks First", []), "2A"
            ),
            "2B": self._amazon_records_from_sheet(
                rows_by_sheet.get("Trending - Earnings First", []), "2B"
            ),
            "2C": self._amazon_records_from_sheet(
                rows_by_sheet.get("Trending - Items Shipped First", []), "2C"
            ),
            "collections": self._amazon_records_from_sheet(
                rows_by_sheet.get("Curated Collections", []), "collection"
            ),
            "all_asins": self._amazon_records_from_sheet(
                rows_by_sheet.get("All Aggregated ASINs", []), "all_asins"
            ),
            "summary_meta": self._parse_summary_tab(
                rows_by_sheet.get("Summary", [])
            ),
        }

    def diagnostics(self, parsed: dict[str, Any]) -> dict[str, Any]:
        collection_names = sorted(
            {r.collection_name for r in parsed.get("collections", []) if r.collection_name}
        )
        file_meta = parse_workbook_filename(self.workbook_path)
        return {
            "workbook_path": str(self.workbook_path),
            "date_label": file_meta["date_label"],
            "source": file_meta["source"],
            "sheet_names_found": self.sheet_names_found,
            "clicks_trend_records": len(parsed.get("2A", [])),
            "earnings_trend_records": len(parsed.get("2B", [])),
            "shipped_trend_records": len(parsed.get("2C", [])),
            "curated_collection_names_found": collection_names,
            "curated_collection_count": len(collection_names),
            "all_asins_count": len(parsed.get("all_asins", [])),
            "summary_meta": parsed.get("summary_meta") or {},
        }

    def _amazon_records_from_sheet(
        self, rows: list[dict[str, str]], source_list_type: str
    ) -> list[AmazonTrendRecord]:
        records = []
        for idx, row in enumerate(rows, start=1):
            asin = str(row.get("ASIN") or "").strip()
            if not asin:
                continue
            raw_link = str(row.get("Amazon Link") or row.get("Product URL") or "").strip()
            amazon_link = _ensure_amazon_tag(raw_link) if raw_link else ""
            records.append(AmazonTrendRecord(
                asin=asin,
                product_title=(row.get("Product Title") or "").strip(),
                amazon_link=amazon_link,
                clicks=_to_int(row.get("Clicks")),
                items_ordered=_to_int(row.get("Items Ordered")),
                items_shipped=_to_int(row.get("Items Shipped")),
                items_returned=_to_int(row.get("Items Returned")),
                items_shipped_revenue=_to_float(row.get("Items Shipped Revenue")),
                items_shipped_earnings=_to_float(row.get("Items Shipped Earnings")),
                total_earnings=_to_float(row.get("Total Earnings")),
                collection_name=(row.get("Collection") or "").strip(),
                source_list_type=source_list_type,
                rank=idx,
            ))
        return records


class AmazonTrendStore:
    """CRUD layer for Amazon trend tables.

    Amazon-specific tables (amazon_trend_products, amazon_product_performance_snapshots,
    amazon_affiliate_links) are managed here directly. Shared tables
    (walmart_refresh_runs, walmart_collections, walmart_collection_items,
    walmart_urlgenius_links) delegate to WalmartTrendStore with appropriate scoping.
    """

    def __init__(self, walmart_store: WalmartTrendStore | None = None):
        self._walmart_store = walmart_store or WalmartTrendStore()

    # --- run lifecycle (delegated) ---

    def create_run(self, source_file: str = "", date_label: str = "") -> int:
        return self._walmart_store.create_run(
            "amazon_workbook_bootstrap",
            source_file,
            date_label=date_label,
        )

    def finish_run(
        self, run_id: int, status: str, counts: dict[str, int], failures: list[dict[str, str]]
    ) -> None:
        self._walmart_store.finish_run(run_id, status, counts, failures)

    def update_run_metadata(self, run_id: int, metadata: dict[str, Any]) -> None:
        self._walmart_store.update_run_metadata(run_id, metadata)

    # --- collections (delegated, retailer-scoped) ---

    def replace_collections(self, run_id: int, collections: list[dict[str, Any]]) -> None:
        self._walmart_store.replace_collections(
            run_id, "amazon_workbook_bootstrap", collections, retailer="amazon"
        )

    # --- URLGenius cache (delegated — shared table, retailer-agnostic) ---

    def urlgenius_for(self, destination_url: str) -> dict[str, str] | None:
        return self._walmart_store.urlgenius_for(destination_url)

    def save_urlgenius_link(
        self,
        destination_url: str,
        genius_url: str,
        link_id: str = "",
        status: str = "active",
        error: str = "",
    ) -> None:
        self._walmart_store.save_urlgenius_link(
            destination_url, genius_url, link_id, status, error
        )

    # --- amazon_trend_products ---

    def upsert_product(self, record: AmazonTrendRecord) -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO amazon_trend_products (asin, product_title, amazon_link, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(asin) DO UPDATE SET
                    product_title = COALESCE(NULLIF(excluded.product_title, ''), amazon_trend_products.product_title),
                    amazon_link   = COALESCE(NULLIF(excluded.amazon_link,   ''), amazon_trend_products.amazon_link),
                    updated_at    = CURRENT_TIMESTAMP
                """,
                (record.asin, record.product_title, record.amazon_link),
            )
            conn.commit()
        finally:
            conn.close()

    def get_product(self, asin: str) -> dict[str, Any] | None:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM amazon_trend_products WHERE asin = ?", (asin,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # --- amazon_product_performance_snapshots ---

    def add_snapshot(self, run_id: int, record: AmazonTrendRecord) -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO amazon_product_performance_snapshots
                (refresh_run_id, asin, source_list_type, collection_name,
                 clicks, items_ordered, items_shipped, items_returned,
                 total_earnings, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    record.asin,
                    record.source_list_type,
                    record.collection_name or "",
                    record.clicks,
                    record.items_ordered,
                    record.items_shipped,
                    record.items_returned,
                    record.total_earnings,
                    record.rank,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # --- amazon_affiliate_links ---

    def seed_workbook_affiliate_link(self, asin: str, amazon_link: str) -> bool:
        """Insert a workbook-sourced link only if no entry exists for this ASIN.

        Never overwrites an existing affiliate link. Returns True if inserted.
        """
        if not asin or not amazon_link:
            return False
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT id FROM amazon_affiliate_links WHERE asin = ? LIMIT 1", (asin,)
            ).fetchone()
            if existing:
                return False
            conn.execute(
                """
                INSERT INTO amazon_affiliate_links
                    (asin, product_url, affiliate_url, status, updated_at)
                VALUES (?, ?, ?, 'workbook', CURRENT_TIMESTAMP)
                ON CONFLICT(asin) DO NOTHING
                """,
                (asin, amazon_link, amazon_link),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def affiliate_link_for(self, asin: str) -> str:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT affiliate_url
                FROM amazon_affiliate_links
                WHERE asin = ?
                ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'workbook' THEN 1 ELSE 2 END,
                         updated_at DESC, id DESC
                LIMIT 1
                """,
                (asin,),
            ).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()


class AmazonCollectionBuilder:
    """Build collection dicts from an Amazon parsed workbook."""

    def from_workbook(self, parsed: dict[str, Any]) -> list[dict[str, Any]]:
        top = self._top_picks(
            parsed.get("2A", []),
            parsed.get("2B", []),
            parsed.get("2C", []),
        )
        grouped: dict[str, list[AmazonTrendRecord]] = defaultdict(list)
        for record in parsed.get("collections", []):
            grouped[record.collection_name].append(record)
        collections = [top]
        for name, records in grouped.items():
            collections.append({
                "slug": _slugify(name, "amazon-collection"),
                "name": name,
                "description": f"Curated Amazon picks for {name}.",
                "metadata": {"source": "amazon_workbook_curated", "target_size": "8-10"},
                "items": [self._item(r) for r in sorted(records, key=lambda r: r.rank or 999)],
            })
        return collections

    def _top_picks(
        self,
        by_clicks: list[AmazonTrendRecord],
        by_earnings: list[AmazonTrendRecord],
        by_shipped: list[AmazonTrendRecord],
    ) -> dict[str, Any]:
        merged: dict[str, dict[str, Any]] = {}
        for source, badge, records in (
            ("2A", "Top by Clicks", by_clicks),
            ("2B", "Top by Earnings", by_earnings),
            ("2C", "Top by Shipped", by_shipped),
        ):
            for record in records:
                item = merged.setdefault(record.asin, self._item(record))
                item.setdefault("badges", [])
                if badge not in item["badges"]:
                    item["badges"].append(badge)
                item.setdefault("metadata", {})[source] = {"rank": record.rank}
        return {
            "slug": "amazon-top-picks",
            "name": "Amazon Top Picks",
            "description": "Amazon products trending by clicks, earnings, and items shipped.",
            "metadata": {"source": "combined_2A_2B_2C", "dedupe": "asin"},
            "items": list(merged.values()),
        }

    def _item(self, record: AmazonTrendRecord) -> dict[str, Any]:
        return {
            "sku": record.asin,  # sku column holds ASIN; retailer='amazon' is the discriminator
            "item_count": record.clicks,
            "sale_amount": record.items_shipped_revenue,
            "total_earnings": record.total_earnings,
            "badges": [],
            "metadata": {"source_list_type": record.source_list_type, "rank": record.rank},
        }


class AmazonTrendRefreshService:
    """Orchestrate a full Amazon workbook bootstrap."""

    def __init__(self) -> None:
        db_schema.bootstrap()
        self.store = AmazonTrendStore()
        self.builder = AmazonCollectionBuilder()

    def bootstrap_from_workbook(
        self, workbook_path: "str | os.PathLike[str]"
    ) -> RefreshResult:
        file_meta = parse_workbook_filename(Path(workbook_path))
        run_id = self.store.create_run(
            str(workbook_path),
            date_label=file_meta["date_label"],
        )
        try:
            parser = AmazonWorkbookParser(workbook_path)
            parsed = parser.parse()
            diagnostics = parser.diagnostics(parsed)

            summary_meta = parsed.get("summary_meta") or {}
            if summary_meta:
                self.store.update_run_metadata(run_id, summary_meta)

            seeded_links = 0
            for record in parsed.get("all_asins", []):
                try:
                    self.store.upsert_product(record)
                    if record.amazon_link:
                        if self.store.seed_workbook_affiliate_link(record.asin, record.amazon_link):
                            seeded_links += 1
                except Exception as exc:
                    logging.warning(
                        "[AMAZON_TRENDS] all_asins upsert failed for %s: %s", record.asin, exc
                    )
            if seeded_links:
                logging.info("[AMAZON_TRENDS] Seeded %d workbook affiliate links", seeded_links)

            trend_records = (
                parsed.get("2A", [])
                + parsed.get("2B", [])
                + parsed.get("2C", [])
                + parsed.get("collections", [])
            )
            if not trend_records and not parsed.get("all_asins"):
                raise WorkbookValidationError(
                    "Workbook parsed successfully but produced no trend records"
                )

            return self._process_records(
                run_id,
                trend_records,
                self.builder.from_workbook(parsed),
                diagnostics=diagnostics,
            )
        except Exception as exc:
            failures = [{"stage": "workbook_parse", "error": str(exc)}]
            self.store.finish_run(run_id, "failed", {"records": 0}, failures)
            return RefreshResult(run_id, "failed", {"records": 0}, failures)

    def _process_records(
        self,
        run_id: int,
        records: list[AmazonTrendRecord],
        collections: list[dict[str, Any]],
        diagnostics: dict[str, Any] | None = None,
    ) -> RefreshResult:
        failures: list[dict[str, str]] = []
        unique_asins = sorted({r.asin for r in records})

        for record in records:
            try:
                self.store.upsert_product(record)
                self.store.add_snapshot(run_id, record)
            except Exception as exc:
                failures.append({"stage": "snapshot", "asin": record.asin, "error": str(exc)})

        for record in records:
            if record.amazon_link:
                try:
                    self.store.seed_workbook_affiliate_link(record.asin, record.amazon_link)
                except Exception as exc:
                    logging.debug(
                        "[AMAZON_TRENDS] affiliate seed skipped for %s: %s", record.asin, exc
                    )

        collection_item_rows = sum(len(c.get("items", [])) for c in collections)
        try:
            self.store.replace_collections(run_id, collections)
        except Exception as exc:
            failures.append({"stage": "collections", "error": str(exc)})

        counts = {
            "records": len(records),
            "unique_asins": len(unique_asins),
            "products_inserted_updated": len(unique_asins),
            "collections": len(collections),
            "collection_item_rows_inserted": collection_item_rows,
            "failures": len(failures),
        }
        logging.info(
            "[AMAZON_TRENDS] bootstrap diagnostics: %s",
            __import__("json").dumps(diagnostics or {}, sort_keys=True),
        )
        status = "partial" if failures else "success"
        self.store.finish_run(run_id, status, counts, failures)
        return RefreshResult(run_id, status, counts, failures, diagnostics or {})
