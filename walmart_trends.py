"""Walmart What's Trending Now refresh workflow.

This module keeps the Walmart trending landing page independent from the
existing Archer/Amazon collage flow while reusing the existing Walmart, Impact,
and URLGenius API clients from product_api.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

import requests

import db_schema
from product_api import ImpactAPI, URLGeniusAPI, WalmartAPI

DB_PATH = db_schema.DB_PATH
DEFAULT_WORKBOOK = Path("attached_assets/Walmart_May6th_Analysis.xlsx")
ATTACHED_ASSETS_DIR = Path("attached_assets")
SHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
STALE_DOUBLE_ENCODED_WALMART_GOTO_ERROR = "stale double-encoded Walmart goto destination"

SAFE_TITLE_BRAND_PREFIXES = (
    ("better homes & gardens", "Better Homes & Gardens"),
    ("best choice products", "Best Choice Products"),
    ("expert gardener", "Expert Gardener"),
    ("sportspower", "Sportspower"),
    ("jumpzylla", "JUMPZYLLA"),
    ("concetta", "CONCETTA"),
    ("mainstays", "Mainstays"),
)
INVALID_BRAND_VALUES = {
    "walmartcreator.com",
    "walmart.com",
    "amazon.com",
    "urlgeni.us",
    "goto.walmart.com",
}


@dataclass
class TrendRecord:
    sku: str
    item_name: str = ""
    category_list: str = ""
    item_count: int = 0
    sale_amount: float = 0.0
    total_earnings: float = 0.0
    source_list_type: str = ""
    collection_name: str = ""
    rank: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    brand: str = ""
    landing_page_url: str = ""


@dataclass
class RefreshResult:
    run_id: int
    status: str
    counts: dict[str, int]
    failures: list[dict[str, str]]
    diagnostics: dict[str, Any] = field(default_factory=dict)


class RefreshAlreadyRunning(RuntimeError):
    """Raised when another Walmart trend refresh is already in progress."""


class WorkbookValidationError(ValueError):
    """Raised when the workbook is missing required sheets or columns."""


def parse_workbook_filename(path: "str | os.PathLike[str]") -> dict[str, str]:
    """Extract source type and date label from a workbook filename.

    Walmart_May12_Analysis.xlsx   → {"source": "Walmart", "date_label": "May12"}
    Amazon_Summer2025_Analysis.xlsx → {"source": "Amazon",  "date_label": "Summer2025"}
    unknown.xlsx                  → {"source": "unknown",  "date_label": ""}

    Rule: split stem on '_', first part is source, second part is date label.
    """
    stem = Path(path).stem
    parts = stem.split("_", 2)
    source = parts[0] if len(parts) >= 2 and parts[0] in ("Walmart", "Amazon") else "unknown"
    date_label = parts[1] if source != "unknown" and len(parts) >= 2 else ""
    return {"source": source, "date_label": date_label}


def discover_workbooks(assets_dir: Path = ATTACHED_ASSETS_DIR) -> list[dict]:
    """Scan assets_dir for .xlsx workbook files.

    Returns a list of dicts sorted newest-modified first:
      filename, path, source, date_label, modified_at (ISO), modified_display
    """
    if not assets_dir.is_dir():
        return []
    workbooks = []
    for p in assets_dir.glob("*.xlsx"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        meta = parse_workbook_filename(p)
        workbooks.append({
            "filename": p.name,
            "path": str(p),
            "source": meta["source"],
            "date_label": meta["date_label"],
            "modified_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S"),
            "modified_display": datetime.fromtimestamp(mtime).strftime("%b %-d, %Y"),
        })
    workbooks.sort(key=lambda w: w["modified_at"], reverse=True)
    return workbooks


def _connect():
    return db_schema._connect()


def _slugify(value: str, fallback: str = "collection") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or fallback


def infer_product_brand_from_title(title: str) -> str:
    """Infer only from known-safe brand prefixes in Walmart product titles."""
    clean_title = re.sub(r"\s+", " ", (title or "").strip())
    if not clean_title:
        return ""
    lower_title = clean_title.lower()
    for prefix, brand in SAFE_TITLE_BRAND_PREFIXES:
        if lower_title == prefix or lower_title.startswith(prefix + " "):
            return brand
    return ""


def normalize_product_brand(value: Any, title: str = "") -> str:
    """Normalize product brands and reject source/domain values."""
    brand = re.sub(r"\s+", " ", str(value or "").strip())
    lower_brand = brand.lower()
    if not brand:
        return infer_product_brand_from_title(title)
    if (
        lower_brand in INVALID_BRAND_VALUES
        or "http" in lower_brand
        or ".com" in lower_brand
        or "urlgeni.us" in lower_brand
    ):
        return infer_product_brand_from_title(title)
    return brand


def _is_double_encoded_walmart_destination(value: str) -> bool:
    """Return True when a URL value still contains an encoded Walmart URL after one decode."""
    if not value:
        return False
    decoded_once = unquote(value)
    return (
        "www.walmart.com" in decoded_once
        and ("https%3A%2F%2F" in decoded_once or "http%3A%2F%2F" in decoded_once)
    )


def is_malformed_double_encoded_walmart_goto(url: str) -> bool:
    """Detect the old broken goto.walmart.com pattern where `u` was encoded twice."""
    if not url or "goto.walmart.com" not in url:
        return False
    parsed = urlparse(url)
    if parsed.netloc.lower() != "goto.walmart.com":
        return False
    raw_query = parsed.query or ""
    if "u=https%253A%252F%252Fwww.walmart.com" in raw_query or "u=http%253A%252F%252Fwww.walmart.com" in raw_query:
        return True
    for value in parse_qs(raw_query, keep_blank_values=True).get("u", []):
        if _is_double_encoded_walmart_destination(value):
            return True
    return False


def stale_walmart_link_reason(url: str) -> str:
    """Return a local-code stale reason for stored Walmart link values."""
    if not url:
        return ""
    if is_malformed_double_encoded_walmart_goto(url):
        return STALE_DOUBLE_ENCODED_WALMART_GOTO_ERROR
    if "https%253A%252F%252Fwww.walmart.com" in url or "http%253A%252F%252Fwww.walmart.com" in url:
        return "stored URL contains a double-encoded Walmart destination"
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc == "goto.walmart.com":
        expected_path = (
            f"/c/{ImpactAPI.WALMART_ACCOUNT_ID}/"
            f"{ImpactAPI.WALMART_REFERRAL_ID}/{ImpactAPI.WALMART_PROGRAM_ID}"
        )
        if parsed.path != expected_path:
            if not parsed.path.startswith("/c/"):
                return "stored Walmart affiliate URL is vanity/non-creator goto path"
            return "stored Walmart affiliate URL uses old creator goto path"
        query = parse_qs(parsed.query, keep_blank_values=True)
        expected_source_id = os.environ.get("WALMART_IMPACT_SOURCE_ID") or ImpactAPI.WALMART_SOURCE_ID
        if query.get("sourceid", [""])[0] != expected_source_id:
            return "stored Walmart affiliate URL missing required sourceid"
        destination = query.get("u", [""])[0]
        destination_reason = ImpactAPI.walmart_destination_stale_reason(destination)
        if destination_reason:
            return destination_reason
    if netloc in {"walmart.com", "www.walmart.com"}:
        return "stored affiliate URL is raw Walmart destination"
    return ""


def extract_walmart_sku_from_url(url: str) -> str:
    """Best-effort SKU extraction from raw or encoded Walmart PDP/goto URLs."""
    value = url or ""
    for _ in range(3):
        match = re.search(r"/ip/(?:[^/?#%]+/)?(\d{5,})", value)
        if match:
            return match.group(1)
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return ""


def _is_numeric_cell(value: str) -> bool:
    """Return True if the string looks like a plain number (int or float), not a label."""
    try:
        float(value.replace(",", "").replace("$", ""))
        return True
    except (ValueError, AttributeError):
        return False


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _price_display(value: Any) -> str:
    amount = _to_float(value)
    return f"${amount:,.2f}" if amount else ""


class WorkbookTrendParser:
    """Parse the workbook with stdlib XLSX XML support.

    Avoiding an openpyxl dependency keeps the Replit app lightweight. The parser
    handles shared strings and simple scalar cell values used by this workbook.
    """

    REQUIRED_SHEETS = {
        "Trending - Item Count First": {"SKU", "Item Name", "Category List", "Item Count", "Sale Amount", "Total Earnings"},
        "Trending - Earnings First": {"SKU", "Item Name", "Category List", "Item Count", "Sale Amount", "Total Earnings"},
        "Curated Collections": {"Collection", "SKU", "Item Name", "Category List", "Item Count", "Sale Amount", "Total Earnings"},
    }
    # Present in newer workbooks — parse if available, skip silently if absent.
    OPTIONAL_SHEETS = {
        "All Aggregated SKUs": {"SKU"},
        "Summary": set(),
    }

    def __init__(self, workbook_path: str | os.PathLike[str] = DEFAULT_WORKBOOK):
        self.workbook_path = Path(workbook_path)
        self.sheet_names_found: list[str] = []

    def parse(self) -> dict[str, list[TrendRecord]]:
        if not self.workbook_path.exists():
            raise FileNotFoundError(f"Workbook not found: {self.workbook_path}")

        rows_by_sheet = self._read_workbook_rows()
        self._validate(rows_by_sheet)
        return {
            "1A": self._records_from_sheet(
                rows_by_sheet.get("Trending - Item Count First", []), "1A"
            ),
            "1B": self._records_from_sheet(
                rows_by_sheet.get("Trending - Earnings First", []), "1B"
            ),
            "collections": self._records_from_sheet(
                rows_by_sheet.get("Curated Collections", []), "collection"
            ),
            "all_skus": self._records_from_aggregated_sheet(
                rows_by_sheet.get("All Aggregated SKUs", [])
            ),
            "summary_meta": self._parse_summary_tab(
                rows_by_sheet.get("Summary", [])
            ),
        }

    def _read_workbook_rows(self) -> dict[str, list[dict[str, str]]]:
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
                    # Summary tab has no column headers — index by position (col0, col1, ...)
                    rows = []
                    for raw in raw_rows:
                        if not any(raw):
                            continue
                        rows.append({f"col{i}": raw[i] for i in range(len(raw))})
                    out[sheet_name] = rows
                    continue
                headers = [h.strip() for h in raw_rows[0]]
                rows = []
                for raw in raw_rows[1:]:
                    if not any(raw):
                        continue
                    rows.append({headers[i]: raw[i] if i < len(raw) else "" for i in range(len(headers))})
                out[sheet_name] = rows
            return out

    def _shared_strings(self, zf: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in zf.namelist():
            return []
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        strings = []
        for si in root.findall("m:si", SHEET_NS):
            strings.append("".join(t.text or "" for t in si.iter(f"{{{SHEET_NS['m']}}}t")))
        return strings

    def _sheet_paths(self, zf: zipfile.ZipFile) -> dict[str, str]:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.attrib["Id"]: rel.attrib["Target"].lstrip("/")
            for rel in rels
            if rel.attrib.get("Target", "").endswith(".xml")
        }
        paths = {}
        for sheet in wb.findall(".//m:sheet", SHEET_NS):
            rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rel_map.get(rid or "")
            if target:
                paths[sheet.attrib["name"]] = target
        return paths

    def _sheet_rows(self, zf: zipfile.ZipFile, path: str, shared: list[str]) -> list[list[str]]:
        root = ET.fromstring(zf.read(path))
        rows = []
        for row in root.findall(".//m:sheetData/m:row", SHEET_NS):
            cells: dict[int, str] = {}
            for cell in row.findall("m:c", SHEET_NS):
                idx = self._col_index(cell.attrib.get("r", "A1"))
                cells[idx] = self._cell_value(cell, shared)
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return rows

    def _cell_value(self, cell: ET.Element, shared: list[str]) -> str:
        value = cell.find("m:v", SHEET_NS)
        if value is None:
            inline = cell.find("m:is/m:t", SHEET_NS)
            return inline.text if inline is not None and inline.text else ""
        raw = value.text or ""
        if cell.attrib.get("t") == "s":
            try:
                return shared[int(raw)]
            except (IndexError, ValueError):
                return ""
        return raw

    def _col_index(self, ref: str) -> int:
        letters = re.sub(r"[^A-Z]", "", ref.upper())
        idx = 0
        for char in letters:
            idx = idx * 26 + (ord(char) - ord("A") + 1)
        return max(idx - 1, 0)

    @staticmethod
    def _find_header_row_index(raw_rows: list[list[str]], key_col: str) -> int:
        """Return the index of the first row containing key_col as an exact cell value.

        Comparison is strip+case-insensitive. Used by subclasses whose sheets prepend a
        title row and/or blank rows before the actual column headers. Falls back to 0
        (treat first row as headers) when key_col is empty or not found.
        """
        if not key_col:
            return 0
        needle = key_col.strip().lower()
        for i, row in enumerate(raw_rows):
            if any(cell.strip().lower() == needle for cell in row):
                return i
        return 0

    def _validate(self, rows_by_sheet: dict[str, list[dict[str, str]]]) -> None:
        missing_sheets = [name for name in self.REQUIRED_SHEETS if name not in rows_by_sheet]
        if missing_sheets:
            raise WorkbookValidationError(f"Workbook missing required sheets: {', '.join(missing_sheets)}")
        missing_columns = []
        for sheet, required in self.REQUIRED_SHEETS.items():
            rows = rows_by_sheet.get(sheet) or []
            present = set(rows[0].keys()) if rows else set()
            missing = sorted(required - present)
            if missing:
                missing_columns.append(f"{sheet}: {', '.join(missing)}")
        if missing_columns:
            raise WorkbookValidationError(f"Workbook missing required columns: {'; '.join(missing_columns)}")

    def diagnostics(self, parsed: dict[str, Any]) -> dict[str, Any]:
        collection_names = sorted({r.collection_name for r in parsed.get("collections", []) if r.collection_name})
        file_meta = parse_workbook_filename(self.workbook_path)
        return {
            "workbook_path": str(self.workbook_path),
            "date_label": file_meta["date_label"],
            "source": file_meta["source"],
            "sheet_names_found": self.sheet_names_found,
            "item_count_trend_records": len(parsed.get("1A", [])),
            "earnings_trend_records": len(parsed.get("1B", [])),
            "curated_collection_names_found": collection_names,
            "curated_collection_count": len(collection_names),
            "all_skus_count": len(parsed.get("all_skus", [])),
            "summary_meta": parsed.get("summary_meta") or {},
        }

    def _records_from_sheet(self, rows: list[dict[str, str]], source_type: str) -> list[TrendRecord]:
        records = []
        for idx, row in enumerate(rows, start=1):
            sku = str(row.get("SKU") or "").strip()
            if not sku:
                continue
            records.append(TrendRecord(
                sku=sku,
                item_name=(row.get("Item Name") or "").strip(),
                category_list=(row.get("Category List") or "").strip(),
                item_count=_to_int(row.get("Item Count")),
                sale_amount=_to_float(row.get("Sale Amount")),
                total_earnings=_to_float(row.get("Total Earnings")),
                source_list_type=source_type,
                collection_name=(row.get("Collection") or "").strip(),
                rank=idx,
            ))
        return records

    def _records_from_aggregated_sheet(self, rows: list[dict[str, str]]) -> list[TrendRecord]:
        """Parse All Aggregated SKUs tab. Captures Brand and Landing Page URL when present."""
        records = []
        for row in rows:
            sku = str(row.get("SKU") or "").strip()
            if not sku:
                continue
            records.append(TrendRecord(
                sku=sku,
                item_name=(row.get("Item Name") or "").strip(),
                category_list=(row.get("Category List") or "").strip(),
                item_count=_to_int(row.get("Item Count")),
                sale_amount=_to_float(row.get("Sale Amount")),
                total_earnings=_to_float(row.get("Total Earnings")),
                source_list_type="all_skus",
                brand=normalize_product_brand(row.get("Brand") or "", row.get("Item Name") or ""),
                landing_page_url=(row.get("Landing Page URL") or "").strip(),
            ))
        return records

    def _parse_summary_tab(self, rows: list[dict[str, str]]) -> dict[str, Any]:
        """Parse Summary tab as key-value pairs. Handles multi-pair rows. Never raises."""
        meta: dict[str, Any] = {}
        if not rows:
            return meta
        for row in rows:
            vals = list(row.values())
            i = 0
            while i < len(vals) - 1:
                key_raw = str(vals[i]).strip()
                val_raw = str(vals[i + 1]).strip()
                if key_raw and not _is_numeric_cell(key_raw) and val_raw:
                    key = key_raw.lower().replace(" ", "_")
                    meta[key] = val_raw
                    i += 2
                else:
                    i += 1
        return meta


class WalmartTrendStore:
    def create_run(self, source_type: str, source_file: str = "", window_start: str = "", window_end: str = "", date_label: str = "") -> int:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                """
                SELECT id, source_type, started_at
                FROM walmart_refresh_runs
                WHERE status = 'running'
                  AND started_at >= datetime('now', '-2 hours')
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            if running:
                conn.rollback()
                raise RefreshAlreadyRunning(
                    f"Walmart trend refresh already running: run_id={running['id']} "
                    f"source_type={running['source_type']} started_at={running['started_at']}"
                )
            conn.execute(
                """
                UPDATE walmart_refresh_runs
                SET status = 'failed',
                    failures_json = '[{"stage":"lock","error":"stale running run expired"}]',
                    finished_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                  AND started_at < datetime('now', '-2 hours')
                """
            )
            cur = conn.execute(
                """
                INSERT INTO walmart_refresh_runs
                (source_type, source_file, window_start, window_end, status, date_label)
                VALUES (?, ?, ?, ?, 'running', ?)
                RETURNING id
                """,
                (source_type, source_file, window_start or None, window_end or None, date_label or None),
            )
            run_id = db_schema._last_id(cur)
            conn.commit()
            return run_id
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def finish_run(self, run_id: int, status: str, counts: dict[str, int], failures: list[dict[str, str]]) -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE walmart_refresh_runs
                SET status = ?, counts_json = ?, failures_json = ?, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, json.dumps(counts), json.dumps(failures), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def upsert_product_from_record(self, record: TrendRecord) -> None:
        normalized_brand = normalize_product_brand(record.brand, record.item_name)
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO walmart_products (sku, item_name, category_list, brand, canonical_url, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sku) DO UPDATE SET
                    item_name = COALESCE(NULLIF(excluded.item_name, ''), walmart_products.item_name),
                    category_list = COALESCE(NULLIF(excluded.category_list, ''), walmart_products.category_list),
                    brand = CASE
                        WHEN NULLIF(excluded.brand, '') IS NOT NULL THEN excluded.brand
                        WHEN lower(COALESCE(walmart_products.brand, '')) LIKE '%walmartcreator%' THEN ''
                        WHEN lower(COALESCE(walmart_products.brand, '')) LIKE '%walmart.com%' THEN ''
                        WHEN lower(COALESCE(walmart_products.brand, '')) LIKE '%urlgeni%' THEN ''
                        WHEN lower(COALESCE(walmart_products.brand, '')) LIKE '%goto.walmart%' THEN ''
                        WHEN lower(COALESCE(walmart_products.brand, '')) LIKE '%http%' THEN ''
                        WHEN lower(COALESCE(walmart_products.brand, '')) LIKE '%.com%' THEN ''
                        ELSE walmart_products.brand
                    END,
                    canonical_url = COALESCE(walmart_products.canonical_url, excluded.canonical_url),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (record.sku, record.item_name, record.category_list, normalized_brand, f"https://www.walmart.com/ip/{record.sku}"),
            )
            conn.commit()
        finally:
            conn.close()

    def add_snapshot(self, run_id: int, source_type: str, record: TrendRecord, window_start: str = "", window_end: str = "") -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO walmart_product_performance_snapshots
                (refresh_run_id, sku, source_type, source_list_type, collection_name,
                 window_start, window_end, item_count, sale_amount, total_earnings, rank, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (refresh_run_id, sku, source_list_type, collection_name) DO UPDATE SET
                  item_count=EXCLUDED.item_count, sale_amount=EXCLUDED.sale_amount,
                  total_earnings=EXCLUDED.total_earnings, rank=EXCLUDED.rank,
                  metadata_json=EXCLUDED.metadata_json
                """,
                (
                    run_id, record.sku, source_type, record.source_list_type, record.collection_name or "",
                    window_start or None, window_end or None, record.item_count, record.sale_amount,
                    record.total_earnings, record.rank, json.dumps(record.metadata or {}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_product(self, sku: str) -> dict[str, Any] | None:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM walmart_products WHERE sku = ?", (sku,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_product_enrichment(self, sku: str, data: dict[str, Any], status: str, error: str = "") -> None:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT brand, product_title, item_name FROM walmart_products WHERE sku = ?",
                (sku,),
            ).fetchone()
            existing_brand = existing["brand"] if existing else ""
            existing_title = (existing["product_title"] or existing["item_name"] or "") if existing else ""
            title = data.get("title") or data.get("name") or existing_title or ""
            new_brand = normalize_product_brand(data.get("brand") or "", title)
            preserved_brand = normalize_product_brand(existing_brand, title or existing_title)
            final_brand = new_brand or preserved_brand
            conn.execute(
                """
                UPDATE walmart_products SET
                    product_title = COALESCE(NULLIF(?, ''), product_title),
                    brand = ?,
                    taxonomy = COALESCE(NULLIF(?, ''), taxonomy),
                    image_url = COALESCE(NULLIF(?, ''), image_url),
                    current_price = COALESCE(?, current_price),
                    price_display = COALESCE(NULLIF(?, ''), price_display),
                    availability = COALESCE(NULLIF(?, ''), availability),
                    rating = COALESCE(?, rating),
                    review_count = COALESCE(?, review_count),
                    canonical_url = COALESCE(NULLIF(?, ''), canonical_url),
                    enrichment_status = ?, enrichment_error = ?, raw_product_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE sku = ?
                """,
                (
                    title,
                    final_brand,
                    data.get("taxonomy") or data.get("category") or "",
                    data.get("image_url") or data.get("imageUrl") or data.get("image") or "",
                    data.get("price_value"),
                    data.get("price_display") or data.get("price") or "",
                    data.get("availability") or data.get("stock") or "",
                    data.get("rating"),
                    data.get("review_count"),
                    data.get("canonical_url") or data.get("url") or "",
                    status,
                    error,
                    json.dumps(data or {}),
                    sku,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def affiliate_link_for(self, sku: str, product_url: str) -> str:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT impact_url
                FROM walmart_affiliate_links
                WHERE sku = ? AND product_url = ? AND status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                LIMIT 1
                """,
                (sku, product_url),
            ).fetchone()
            return row['impact_url'] if row else ""
        finally:
            conn.close()

    def mark_affiliate_link_stale(self, sku: str, product_url: str, reason: str) -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE walmart_affiliate_links
                SET status = 'stale', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE sku = ? AND product_url = ? AND status IN ('active', 'fallback')
                """,
                (reason, sku, product_url),
            )
            conn.commit()
        finally:
            conn.close()

    def active_affiliate_links_for_sku(self, sku: str) -> list[dict[str, Any]]:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT id, sku, product_url, impact_url, status, error, updated_at
                FROM walmart_affiliate_links
                WHERE sku = ? AND status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                """,
                (sku,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def retire_active_affiliate_links_for_sku(self, sku: str, reason: str) -> int:
        """Mark active/fallback affiliate rows inactive and free their unique product_url keys."""
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT id, product_url
                FROM walmart_affiliate_links
                WHERE sku = ? AND status IN ('active', 'fallback')
                """,
                (sku,),
            ).fetchall()
            for row in rows:
                product_url = row["product_url"] or ""
                archived_product_url = product_url
                if f"#stale-affiliate-{row['id']}" not in archived_product_url:
                    archived_product_url = f"{product_url}#stale-affiliate-{row['id']}"
                conn.execute(
                    """
                    UPDATE walmart_affiliate_links
                    SET status = 'stale', error = ?, product_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (reason, archived_product_url, row["id"]),
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def save_affiliate_link(self, sku: str, product_url: str, impact_url: str, status: str = "active", error: str = "") -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO walmart_affiliate_links (sku, product_url, impact_url, status, error, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sku, product_url) DO UPDATE SET
                    impact_url = excluded.impact_url,
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (sku, product_url, impact_url, status, error),
            )
            conn.commit()
        finally:
            conn.close()

    def seed_workbook_affiliate_link(self, sku: str, landing_page_url: str) -> bool:
        """Insert a workbook-sourced URL as a last-resort affiliate link.

        Only inserts if no active or fallback link exists for this SKU.
        Never overwrites Impact-generated links. Returns True if inserted.
        """
        if not sku or not landing_page_url:
            return False
        conn = _connect()
        try:
            existing = conn.execute(
                """
                SELECT id FROM walmart_affiliate_links
                WHERE sku = ? AND status IN ('active', 'fallback')
                LIMIT 1
                """,
                (sku,),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                """
                INSERT INTO walmart_affiliate_links
                    (sku, product_url, impact_url, status, updated_at)
                VALUES (?, ?, ?, 'workbook', CURRENT_TIMESTAMP)
                ON CONFLICT(sku, product_url) DO NOTHING
                """,
                (sku, landing_page_url, landing_page_url),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def update_run_metadata(self, run_id: int, metadata: dict[str, Any]) -> None:
        """Store summary-tab metadata on the run record."""
        conn = _connect()
        try:
            conn.execute(
                "UPDATE walmart_refresh_runs SET run_metadata_json = ? WHERE id = ?",
                (json.dumps(metadata), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def urlgenius_for(self, destination_url: str) -> dict[str, str] | None:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT destination_url, genius_url, link_id, status
                FROM walmart_urlgenius_links
                WHERE destination_url = ? AND status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                LIMIT 1
                """,
                (destination_url,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def mark_urlgenius_link_stale(self, destination_url: str, reason: str) -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE walmart_urlgenius_links
                SET status = 'stale', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE destination_url = ? AND status IN ('active', 'fallback')
                """,
                (reason, destination_url),
            )
            conn.commit()
        finally:
            conn.close()

    def active_urlgenius_links_for_sku(self, sku: str) -> list[dict[str, Any]]:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT ug.id, ug.destination_url, ug.genius_url, ug.link_id, ug.status, ug.error, ug.updated_at, al.sku AS linked_sku
                FROM walmart_urlgenius_links ug
                LEFT JOIN walmart_affiliate_links al ON al.impact_url = ug.destination_url
                WHERE ug.status IN ('active', 'fallback')
                ORDER BY CASE ug.status WHEN 'active' THEN 0 ELSE 1 END, ug.updated_at DESC, ug.id DESC
                """
            ).fetchall()
            matches = []
            for row in rows:
                rd = dict(row)
                if rd.get("linked_sku") == str(sku) or extract_walmart_sku_from_url(rd.get("destination_url") or "") == str(sku):
                    matches.append(rd)
            return matches
        finally:
            conn.close()

    def retire_urlgenius_links_for_destinations(self, destination_urls: Iterable[str], reason: str) -> int:
        destinations = [url for url in dict.fromkeys(destination_urls) if url]
        if not destinations:
            return 0
        conn = _connect()
        try:
            rows = []
            for destination_url in destinations:
                rows.extend(conn.execute(
                    """
                    SELECT id, destination_url
                    FROM walmart_urlgenius_links
                    WHERE destination_url = ? AND status IN ('active', 'fallback')
                    """,
                    (destination_url,),
                ).fetchall())
            for row in rows:
                destination_url = row["destination_url"] or ""
                archived_destination_url = destination_url
                if f"#stale-urlgenius-{row['id']}" not in archived_destination_url:
                    archived_destination_url = f"{destination_url}#stale-urlgenius-{row['id']}"
                conn.execute(
                    """
                    UPDATE walmart_urlgenius_links
                    SET status = 'stale', error = ?, destination_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (reason, archived_destination_url, row["id"]),
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def current_affiliate_link_for_sku(self, sku: str) -> dict[str, Any] | None:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT sku, product_url, impact_url, status, error, updated_at
                FROM walmart_affiliate_links
                WHERE sku = ? AND status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                LIMIT 1
                """,
                (sku,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def current_urlgenius_for_destination(self, destination_url: str) -> dict[str, Any] | None:
        if not destination_url:
            return None
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT destination_url, genius_url, link_id, status, error, updated_at
                FROM walmart_urlgenius_links
                WHERE destination_url = ? AND status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                LIMIT 1
                """,
                (destination_url,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def stale_urlgenius_for_sku(self, sku: str) -> dict[str, Any] | None:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT destination_url, genius_url, link_id, status, error, updated_at
                FROM walmart_urlgenius_links
                WHERE status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                """
            ).fetchall()
            for row in rows:
                destination_url = row["destination_url"] or ""
                if stale_walmart_link_reason(destination_url) and extract_walmart_sku_from_url(destination_url) == str(sku):
                    return dict(row)
            return None
        finally:
            conn.close()

    def product_url_for_sku(self, sku: str) -> str:
        conn = _connect()
        try:
            product = conn.execute(
                "SELECT canonical_url FROM walmart_products WHERE sku = ?",
                (sku,),
            ).fetchone()
            if product and product["canonical_url"]:
                return product["canonical_url"]
            affiliate = conn.execute(
                """
                SELECT product_url
                FROM walmart_affiliate_links
                WHERE sku = ?
                ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'fallback' THEN 1 WHEN 'stale' THEN 2 ELSE 3 END, updated_at DESC, id DESC
                LIMIT 1
                """,
                (sku,),
            ).fetchone()
            if affiliate and affiliate["product_url"]:
                return affiliate["product_url"]
            return f"https://www.walmart.com/ip/{sku}"
        finally:
            conn.close()

    def stale_walmart_skus(self) -> list[str]:
        conn = _connect()
        try:
            skus: set[str] = set()
            affiliate_rows = conn.execute(
                """
                SELECT sku, impact_url
                FROM walmart_affiliate_links
                WHERE status IN ('active', 'fallback')
                """
            ).fetchall()
            for row in affiliate_rows:
                if stale_walmart_link_reason(row["impact_url"]):
                    skus.add(row["sku"])

            genius_rows = conn.execute(
                """
                SELECT ug.destination_url, al.sku AS linked_sku
                FROM walmart_urlgenius_links ug
                LEFT JOIN walmart_affiliate_links al ON al.impact_url = ug.destination_url
                WHERE ug.status IN ('active', 'fallback')
                """
            ).fetchall()
            for row in genius_rows:
                if not stale_walmart_link_reason(row["destination_url"]):
                    continue
                linked_sku = row["linked_sku"] or extract_walmart_sku_from_url(row["destination_url"])
                if linked_sku:
                    skus.add(str(linked_sku))
            return sorted(skus)
        finally:
            conn.close()

    def current_walmart_skus(self) -> list[str]:
        """Return all SKUs represented in current Walmart product, link, or active collection data."""
        conn = _connect()
        try:
            skus: set[str] = set()
            for row in conn.execute("SELECT sku FROM walmart_products WHERE sku IS NOT NULL AND sku != ''").fetchall():
                skus.add(str(row["sku"]))
            for row in conn.execute(
                """
                SELECT DISTINCT ci.sku
                FROM walmart_collection_items ci
                JOIN walmart_collections c ON c.slug = ci.collection_slug
                WHERE c.is_active = 1 AND ci.sku IS NOT NULL AND ci.sku != ''
                """
            ).fetchall():
                skus.add(str(row["sku"]))
            for row in conn.execute(
                """
                SELECT sku
                FROM walmart_affiliate_links
                WHERE status IN ('active', 'fallback') AND sku IS NOT NULL AND sku != ''
                """
            ).fetchall():
                skus.add(str(row["sku"]))
            genius_rows = conn.execute(
                """
                SELECT ug.destination_url, al.sku AS linked_sku
                FROM walmart_urlgenius_links ug
                LEFT JOIN walmart_affiliate_links al ON al.impact_url = ug.destination_url
                WHERE ug.status IN ('active', 'fallback')
                """
            ).fetchall()
            for row in genius_rows:
                linked_sku = row["linked_sku"] or extract_walmart_sku_from_url(row["destination_url"] or "")
                if linked_sku:
                    skus.add(str(linked_sku))
            return sorted(skus)
        finally:
            conn.close()

    def walmart_link_diagnostics_for_sku(self, sku: str) -> dict[str, Any]:
        affiliate = self.current_affiliate_link_for_sku(sku) or {}
        impact_url = affiliate.get("impact_url") or ""
        genius = self.current_urlgenius_for_destination(impact_url) if impact_url else None
        if not genius:
            genius = self.stale_urlgenius_for_sku(sku)
        genius = genius or {}
        destination_url = genius.get("destination_url") or ""
        affiliate_reason = stale_walmart_link_reason(impact_url)
        urlgenius_reason = stale_walmart_link_reason(destination_url)
        return {
            "sku": sku,
            "product_url": affiliate.get("product_url") or self.product_url_for_sku(sku),
            "impact_url": impact_url,
            "affiliate_url": impact_url,
            "affiliate_status": affiliate.get("status") or "",
            "affiliate_stale": bool(affiliate_reason),
            "affiliate_stale_reason": affiliate_reason,
            "destination_url": destination_url,
            "genius_url": genius.get("genius_url") or "",
            "urlgenius_status": genius.get("status") or "",
            "urlgenius_stale": bool(urlgenius_reason),
            "urlgenius_stale_reason": urlgenius_reason,
            "has_double_encoded_goto": bool(affiliate_reason or urlgenius_reason),
        }

    def save_urlgenius_link(self, destination_url: str, genius_url: str, link_id: str = "", status: str = "active", error: str = "") -> None:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO walmart_urlgenius_links (destination_url, genius_url, link_id, status, error, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(destination_url) DO UPDATE SET
                    genius_url = excluded.genius_url,
                    link_id = excluded.link_id,
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (destination_url, genius_url, link_id, status, error),
            )
            conn.commit()
        finally:
            conn.close()

    def replace_collections(
        self,
        run_id: int,
        source_type: str,
        collections: list[dict[str, Any]],
        retailer: str = "walmart",
    ) -> None:
        conn = _connect()
        try:
            # Scope deactivation to this retailer only — never wipe cross-retailer collections.
            conn.execute(
                "UPDATE walmart_collections SET is_active = 0 WHERE is_active = 1 AND (retailer = ? OR retailer IS NULL)",
                (retailer,),
            )
            for order, collection in enumerate(collections, start=1):
                slug = collection["slug"]
                conn.execute(
                    """
                    INSERT INTO walmart_collections
                    (slug, name, description, source_type, refresh_run_id, display_order,
                     is_active, metadata_json, retailer, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(slug) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        source_type = excluded.source_type,
                        refresh_run_id = excluded.refresh_run_id,
                        display_order = excluded.display_order,
                        is_active = 1,
                        metadata_json = excluded.metadata_json,
                        retailer = excluded.retailer,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        slug, collection["name"], collection.get("description", ""), source_type,
                        run_id, order, json.dumps(collection.get("metadata", {})), retailer,
                    ),
                )
                conn.execute("DELETE FROM walmart_collection_items WHERE collection_slug = ?", (slug,))
                for item_order, item in enumerate(collection.get("items", []), start=1):
                    conn.execute(
                        """
                        INSERT INTO walmart_collection_items
                        (collection_slug, sku, refresh_run_id, display_order, item_count,
                         sale_amount, total_earnings, badges_json, metadata_json, retailer, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT (collection_slug, sku) DO UPDATE SET
                          refresh_run_id=EXCLUDED.refresh_run_id, display_order=EXCLUDED.display_order,
                          item_count=EXCLUDED.item_count, sale_amount=EXCLUDED.sale_amount,
                          total_earnings=EXCLUDED.total_earnings, badges_json=EXCLUDED.badges_json,
                          metadata_json=EXCLUDED.metadata_json, retailer=EXCLUDED.retailer,
                          updated_at=CURRENT_TIMESTAMP
                        """,
                        (
                            slug, item["sku"], run_id, item_order, item.get("item_count", 0),
                            item.get("sale_amount", 0.0), item.get("total_earnings", 0.0),
                            json.dumps(item.get("badges", [])), json.dumps(item.get("metadata", {})),
                            retailer,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def active_collection_diagnostics(self) -> dict[str, Any]:
        conn = _connect()
        try:
            active_count = conn.execute(
                "SELECT COUNT(*) AS c FROM walmart_collections WHERE is_active = 1"
            ).fetchone()["c"]
            first = conn.execute(
                """
                SELECT slug
                FROM walmart_collections
                WHERE is_active = 1
                ORDER BY display_order ASC, name ASC
                LIMIT 1
                """
            ).fetchone()
            first_slug = first["slug"] if first else ""
            first_skus = []
            if first_slug:
                rows = conn.execute(
                    """
                    SELECT sku
                    FROM walmart_collection_items
                    WHERE collection_slug = ?
                    ORDER BY display_order ASC
                    LIMIT 3
                    """,
                    (first_slug,),
                ).fetchall()
                first_skus = [row["sku"] for row in rows]
            return {
                "active_collection_count": active_count,
                "first_active_collection_slug": first_slug,
                "first_active_collection_first_3_skus": first_skus,
            }
        finally:
            conn.close()

    def landing_page_data(self) -> dict[str, Any]:
        conn = _connect()
        try:
            run = conn.execute(
                "SELECT * FROM walmart_refresh_runs WHERE status IN ('success', 'partial') ORDER BY finished_at DESC, id DESC LIMIT 1"
            ).fetchone()
            collection_rows = conn.execute(
                "SELECT * FROM walmart_collections WHERE is_active = 1 ORDER BY display_order ASC, name ASC"
            ).fetchall()
            collections = []
            for collection in collection_rows:
                cd = dict(collection)
                collection_retailer = cd.get("retailer") or "walmart"
                item_rows = conn.execute(
                    """
                    SELECT *
                    FROM walmart_collection_items
                    WHERE collection_slug = ?
                    ORDER BY display_order ASC
                    """,
                    (cd["slug"],),
                ).fetchall()
                items = []
                for row in item_rows:
                    ci = dict(row)
                    item_retailer = ci.get("retailer") or "walmart"
                    if item_retailer == "amazon":
                        built = self._build_amazon_item(conn, ci)
                    else:
                        built = self._build_walmart_item(conn, ci)
                    if built:
                        items.append(built)
                collections.append({
                    "slug": cd["slug"],
                    "name": cd["name"],
                    "description": cd.get("description") or "",
                    "retailer": collection_retailer,
                    "items": items,
                    "metadata": json.loads(cd.get("metadata_json") or "{}"),
                })
            return {
                "last_run": dict(run) if run else None,
                "last_refreshed": run["finished_at"] if run else "",
                "collections": collections,
            }
        finally:
            conn.close()

    def _build_walmart_item(self, conn, ci: dict[str, Any]) -> dict[str, Any] | None:
        sku = ci.get("sku") or ""
        product = conn.execute(
            "SELECT * FROM walmart_products WHERE sku = ?", (sku,)
        ).fetchone()
        if not product:
            return None
        rd = dict(product)
        affiliate = conn.execute(
            """
            SELECT impact_url
            FROM walmart_affiliate_links
            WHERE sku = ? AND status IN ('active', 'fallback', 'workbook')
            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'fallback' THEN 1 ELSE 2 END,
                     updated_at DESC, id DESC
            LIMIT 1
            """,
            (sku,),
        ).fetchone()
        impact_url = affiliate["impact_url"] if affiliate else ""
        genius = None
        if impact_url:
            genius = conn.execute(
                """
                SELECT genius_url
                FROM walmart_urlgenius_links
                WHERE destination_url = ? AND status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                LIMIT 1
                """,
                (impact_url,),
            ).fetchone()
        genius_url = genius["genius_url"] if genius else ""
        badges = json.loads(ci.get("badges_json") or "[]")
        title = rd.get("product_title") or rd.get("item_name") or "Walmart find"
        return {
            "sku": sku,
            "retailer": "walmart",
            "title": title,
            "brand": normalize_product_brand(rd.get("brand") or "", title),
            "category": rd.get("category_list") or rd.get("taxonomy") or "",
            "image_url": rd.get("image_url") or "",
            "price_display": rd.get("price_display") or _price_display(rd.get("current_price")),
            "availability": rd.get("availability") or "",
            "rating": rd.get("rating"),
            "review_count": rd.get("review_count"),
            "item_count": ci.get("item_count") or 0,
            "sale_amount": ci.get("sale_amount") or 0,
            "total_earnings": ci.get("total_earnings") or 0,
            "badges": badges,
            "shop_url": genius_url or impact_url or rd.get("canonical_url") or f"https://www.walmart.com/ip/{sku}",
        }

    def _build_amazon_item(self, conn, ci: dict[str, Any]) -> dict[str, Any] | None:
        asin = ci.get("sku") or ""  # sku column holds ASIN for retailer='amazon' rows
        product = conn.execute(
            "SELECT * FROM amazon_trend_products WHERE asin = ?", (asin,)
        ).fetchone()
        rd = dict(product) if product else {}
        affiliate = conn.execute(
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
        affiliate_url = affiliate["affiliate_url"] if affiliate else ""
        if not affiliate_url:
            affiliate_url = rd.get("amazon_link") or ""
        genius = None
        if affiliate_url:
            genius = conn.execute(
                """
                SELECT genius_url
                FROM walmart_urlgenius_links
                WHERE destination_url = ? AND status IN ('active', 'fallback')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                LIMIT 1
                """,
                (affiliate_url,),
            ).fetchone()
        genius_url = genius["genius_url"] if genius else ""
        badges = json.loads(ci.get("badges_json") or "[]")
        return {
            "sku": asin,
            "retailer": "amazon",
            "title": rd.get("product_title") or "Amazon find",
            "brand": rd.get("brand") or "",
            "category": rd.get("category") or "",
            "image_url": rd.get("image_url") or "",
            "price_display": rd.get("price_display") or _price_display(rd.get("current_price")),
            "availability": "",
            "rating": None,
            "review_count": None,
            "item_count": ci.get("item_count") or 0,
            "sale_amount": ci.get("sale_amount") or 0,
            "total_earnings": ci.get("total_earnings") or 0,
            "badges": badges,
            "shop_url": genius_url or affiliate_url or f"https://www.amazon.com/dp/{asin}",
        }


class WalmartProductEnricher:
    def __init__(self, store: WalmartTrendStore):
        self.store = store
        self.client = WalmartAPI()

    def enrich(self, sku: str) -> dict[str, Any]:
        existing = self.store.get_product(sku) or {}
        existing_title = existing.get("product_title") or existing.get("item_name") or ""
        existing_brand = existing.get("brand") or ""
        existing_brand_normalized = normalize_product_brand(existing_brand, existing_title)
        brand_needs_normalization = existing_brand != existing_brand_normalized
        if (
            existing.get("enrichment_status") == "ok"
            and existing.get("image_url")
            and existing.get("canonical_url")
            and not brand_needs_normalization
        ):
            return existing
        try:
            item = self.client.get_item_by_id(sku)
            if not item:
                fallback = self._fallback(existing, "Walmart API returned no product")
                self.store.update_product_enrichment(sku, fallback, "fallback", fallback["error"])
                return fallback
            price_value = _to_float(item.get("price"))
            title = item.get("name") or existing.get("item_name") or ""
            normalized = {
                "title": title,
                "image_url": item.get("imageUrl") or item.get("image") or existing.get("image_url") or "",
                "price_value": price_value or None,
                "price_display": _price_display(price_value) or str(item.get("price") or ""),
                "canonical_url": item.get("url") or existing.get("canonical_url") or f"https://www.walmart.com/ip/{sku}",
                "category": item.get("category") or existing.get("category_list") or "",
                "brand": normalize_product_brand(item.get("brand") or "", title),
                "availability": item.get("availability") or "",
                "rating": item.get("rating"),
                "review_count": item.get("review_count"),
            }
            self.store.update_product_enrichment(sku, normalized, "ok")
            return normalized
        except Exception as exc:
            logging.warning("[WALMART_TRENDS] enrichment failed for %s: %s", sku, exc)
            fallback = self._fallback(existing, str(exc))
            self.store.update_product_enrichment(sku, fallback, "fallback", str(exc))
            return fallback

    def _fallback(self, existing: dict[str, Any], error: str) -> dict[str, Any]:
        sku = existing.get("sku") or ""
        title = existing.get("product_title") or existing.get("item_name") or "Walmart find"
        return {
            "title": title,
            "image_url": existing.get("image_url") or "",
            "price_value": existing.get("current_price"),
            "price_display": existing.get("price_display") or "",
            "canonical_url": existing.get("canonical_url") or f"https://www.walmart.com/ip/{sku}",
            "category": existing.get("category_list") or "",
            "brand": normalize_product_brand(existing.get("brand") or "", title),
            "error": error,
        }


class AffiliateLinkService:
    def __init__(self, store: WalmartTrendStore):
        self.store = store
        self.client = ImpactAPI()

    def ensure(self, sku: str, product_url: str, sub_id1: str = "walmart-trending", sub_id3: str = None) -> str:
        existing = self.store.affiliate_link_for(sku, product_url)
        if existing:
            stale_reason = stale_walmart_link_reason(existing)
            if stale_reason:
                self.store.mark_affiliate_link_stale(sku, product_url, stale_reason)
            else:
                return existing
        impact_url = self.client.generate_walmart_link(product_url, sku, sub_id1=sub_id1, sub_id2=sku, sub_id3=sub_id3)
        self.store.save_affiliate_link(sku, product_url, impact_url)
        return impact_url


class URLGeniusLinkService:
    def __init__(self, store: WalmartTrendStore):
        self.store = store
        self.client = URLGeniusAPI()

    def ensure(self, destination_url: str, sku: str, force_new: bool = False) -> str:
        existing = None if force_new else self.store.urlgenius_for(destination_url)
        if existing:
            stale_reason = self._stale_reason(existing)
            if stale_reason:
                self.store.mark_urlgenius_link_stale(destination_url, stale_reason)
            else:
                return existing.get("genius_url") or destination_url
        if not self.client.api_key:
            self.store.save_urlgenius_link(destination_url, destination_url, status="fallback", error="URLGENIUS_API_KEY not set")
            return destination_url
        try:
            result = self.client.create_link(
                destination_url,
                utm_source="walmart",
                utm_medium="affiliate",
                utm_campaign="whats-trending-now",
                utm_content=sku,
                force_new=force_new or bool(existing),
            )
            link = result.get("link", {}) if isinstance(result, dict) else {}
            genius_url = link.get("genius_url") or link.get("short_url") or destination_url
            self.store.save_urlgenius_link(destination_url, genius_url, str(link.get("id") or ""))
            return genius_url
        except Exception as exc:
            logging.warning("[WALMART_TRENDS] URLGenius failed for %s: %s", sku, exc)
            self.store.save_urlgenius_link(destination_url, destination_url, status="fallback", error=str(exc))
            return destination_url

    def _stale_reason(self, row: dict[str, str]) -> str:
        destination_url = row.get("destination_url") or ""
        genius_url = row.get("genius_url") or ""
        destination_reason = stale_walmart_link_reason(destination_url)
        if destination_reason:
            return destination_reason
        first_hop = self._first_hop_redirect(genius_url)
        first_hop_reason = stale_walmart_link_reason(first_hop)
        if first_hop_reason:
            return f"{first_hop_reason} in URLGenius first-hop redirect"
        return ""

    def _first_hop_redirect(self, genius_url: str) -> str:
        if not genius_url:
            return ""
        parsed = urlparse(genius_url)
        if not parsed.netloc.lower().endswith("urlgeni.us"):
            return ""
        try:
            response = requests.head(genius_url, allow_redirects=False, timeout=10)
            location = response.headers.get("Location") or response.headers.get("location") or ""
            if response.status_code in {405, 501} or not location:
                response = requests.get(genius_url, allow_redirects=False, timeout=10, stream=True)
                location = response.headers.get("Location") or response.headers.get("location") or ""
            return location
        except Exception as exc:
            logging.info("[WALMART_TRENDS] URLGenius first-hop check skipped for %s: %s", genius_url, exc)
            return ""


class WalmartLinkRegenerationService:
    def __init__(self, store: WalmartTrendStore | None = None):
        self.store = store or WalmartTrendStore()
        self.affiliates = AffiliateLinkService(self.store)
        self.urlgenius = URLGeniusLinkService(self.store)

    def inspect_sku(self, sku: str, include_redirect: bool = False) -> dict[str, Any]:
        sku = str(sku or "").strip()
        if not sku:
            raise ValueError("sku is required")
        diagnostics = self.store.walmart_link_diagnostics_for_sku(sku)
        if include_redirect and diagnostics.get("genius_url"):
            first_hop = self.urlgenius._first_hop_redirect(diagnostics["genius_url"])
            diagnostics["urlgenius_first_hop"] = first_hop
            first_hop_reason = stale_walmart_link_reason(first_hop)
            if first_hop_reason and not diagnostics.get("urlgenius_stale"):
                diagnostics["urlgenius_stale"] = True
                diagnostics["urlgenius_stale_reason"] = f"{first_hop_reason} in URLGenius first-hop redirect"
                diagnostics["has_double_encoded_goto"] = True
        diagnostics["stale"] = bool(diagnostics.get("affiliate_stale") or diagnostics.get("urlgenius_stale"))
        diagnostics["stale_reasons"] = [
            reason for reason in (
                diagnostics.get("affiliate_stale_reason"),
                diagnostics.get("urlgenius_stale_reason"),
            ) if reason
        ]
        return diagnostics

    def regenerate_sku(self, sku: str, force: bool = False, include_redirect: bool = False) -> dict[str, Any]:
        before = self.inspect_sku(sku, include_redirect=include_redirect)
        should_regenerate = force or before["stale"]
        result: dict[str, Any] = {
            "sku": before["sku"],
            "changed": False,
            "before": before,
            "after": before,
            "actions": [],
        }
        if not should_regenerate:
            result["message"] = "No stale Walmart link rows found for SKU"
            return result

        product_url = before.get("product_url") or self.store.product_url_for_sku(before["sku"])
        stale_reason = "; ".join(before.get("stale_reasons") or []) or "forced Walmart link regeneration"
        if before.get("impact_url"):
            self.store.mark_affiliate_link_stale(before["sku"], product_url, stale_reason)
            result["actions"].append("marked affiliate row stale")
        if before.get("destination_url"):
            self.store.mark_urlgenius_link_stale(before["destination_url"], stale_reason)
            result["actions"].append("marked URLGenius row stale")

        fresh_impact_url = self.affiliates.ensure(before["sku"], product_url)
        fresh_genius_url = self.urlgenius.ensure(fresh_impact_url, before["sku"])
        after = self.inspect_sku(before["sku"], include_redirect=False)
        result.update({
            "changed": True,
            "product_url": product_url,
            "fresh_impact_url": fresh_impact_url,
            "fresh_genius_url": fresh_genius_url,
            "after": after,
        })
        result["actions"].extend(["created fresh Impact affiliate link", "created fresh URLGenius link"])
        return result

    def regenerate_all_stale(self, limit: int | None = None, include_redirect: bool = False) -> dict[str, Any]:
        skus = self.store.stale_walmart_skus()
        if limit is not None:
            skus = skus[: max(0, int(limit))]
        results = [self.regenerate_sku(sku, include_redirect=include_redirect) for sku in skus]
        return {
            "status": "ok",
            "stale_skus_found": len(skus),
            "regenerated_count": sum(1 for row in results if row.get("changed")),
            "results": results,
        }

    def rebuild_all(self, limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        all_skus = self.store.current_walmart_skus()
        skus = all_skus[: max(0, int(limit))] if limit is not None else all_skus
        results = [self._rebuild_sku(sku, dry_run=dry_run) for sku in skus]
        return {
            "status": "dry_run" if dry_run else "ok",
            "dry_run": dry_run,
            "total_target_skus": len(all_skus),
            "selected_skus": len(skus),
            "rebuilt_count": 0 if dry_run else sum(1 for row in results if row.get("changed")),
            "results": results,
        }

    def _rebuild_sku(self, sku: str, dry_run: bool = False) -> dict[str, Any]:
        sku = str(sku or "").strip()
        product_url = self.store.product_url_for_sku(sku)
        active_affiliates = self.store.active_affiliate_links_for_sku(sku)
        active_urlgenius = self.store.active_urlgenius_links_for_sku(sku)
        destinations_to_retire = [row.get("impact_url") for row in active_affiliates] + [
            row.get("destination_url") for row in active_urlgenius
        ]
        result: dict[str, Any] = {
            "sku": sku,
            "product_url": product_url,
            "dry_run": dry_run,
            "changed": False,
            "active_affiliate_rows_found": len(active_affiliates),
            "active_urlgenius_rows_found": len(active_urlgenius),
            "actions": [],
        }
        if dry_run:
            result["actions"].extend([
                "would mark active/fallback affiliate rows stale",
                "would mark active/fallback URLGenius rows stale",
                "would create fresh Impact TrackingLinks affiliate link",
                "would create fresh URLGenius link wrapping fresh affiliate link",
            ])
            return result

        reason = "forced full Walmart link rebuild"
        retired_affiliate_count = self.store.retire_active_affiliate_links_for_sku(sku, reason)
        retired_urlgenius_count = self.store.retire_urlgenius_links_for_destinations(destinations_to_retire, reason)
        fresh_impact_url = self.affiliates.client.generate_walmart_link(
            product_url, sku, sub_id1="walmart-trending", sub_id2=sku
        )
        self.store.save_affiliate_link(sku, product_url, fresh_impact_url)
        fresh_genius_url = self.urlgenius.ensure(fresh_impact_url, sku, force_new=True)
        result.update({
            "changed": True,
            "retired_affiliate_rows": retired_affiliate_count,
            "retired_urlgenius_rows": retired_urlgenius_count,
            "fresh_impact_url": fresh_impact_url,
            "fresh_genius_url": fresh_genius_url,
            "after": self.inspect_sku(sku, include_redirect=False),
        })
        result["actions"].extend([
            "marked active/fallback affiliate rows stale",
            "marked active/fallback URLGenius rows stale",
            "created fresh Impact TrackingLinks affiliate link",
            "created fresh URLGenius link wrapping fresh affiliate link",
        ])
        return result


class CollectionBuilder:
    def from_workbook(self, parsed: dict[str, list[TrendRecord]]) -> list[dict[str, Any]]:
        top = self._top_sellers(parsed.get("1A", []), parsed.get("1B", []))
        grouped: dict[str, list[TrendRecord]] = defaultdict(list)
        for record in parsed.get("collections", []):
            grouped[record.collection_name].append(record)
        collections = [top]
        for name, records in grouped.items():
            collections.append({
                "slug": _slugify(name),
                "name": name,
                "description": self._description_for(name, records),
                "metadata": {"source": "workbook_curated", "target_size": "8-10"},
                "items": [self._item(r) for r in sorted(records, key=lambda r: r.rank or 999)],
            })
        return collections

    def from_weekly_records(self, records: list[TrendRecord]) -> tuple[list[TrendRecord], list[TrendRecord], list[dict[str, Any]]]:
        by_units = sorted(records, key=lambda r: (-r.item_count, -r.total_earnings, r.item_name))[:10]
        by_earnings = sorted(records, key=lambda r: (-r.total_earnings, -r.item_count, r.item_name))[:10]
        for idx, record in enumerate(by_units, start=1):
            record.source_list_type = "1A"
            record.rank = idx
        for idx, record in enumerate(by_earnings, start=1):
            record.source_list_type = "1B"
            record.rank = idx
        collections = [self._top_sellers(by_units, by_earnings)]
        clusters: dict[str, list[TrendRecord]] = defaultdict(list)
        for record in records:
            clusters[record.category_list or "Trending Finds"].append(record)
        ranked_clusters = sorted(
            clusters.items(),
            key=lambda kv: (len(kv[1]), sum(r.total_earnings for r in kv[1]), sum(r.item_count for r in kv[1])),
            reverse=True,
        )[:10]
        for category, cluster in ranked_clusters:
            picks = sorted(cluster, key=lambda r: (-r.total_earnings, -r.item_count, r.item_name))[:10]
            if len(picks) < 4:
                continue
            name = self._weekly_collection_name(category)
            collections.append({
                "slug": _slugify(name),
                "name": name,
                "description": f"Fast-moving Walmart picks in {category} from the latest 7-day window.",
                "metadata": {"source": "impact_category_cluster", "category": category, "target_size": "8-10"},
                "items": [self._item(r) for r in picks[:10]],
            })
        return by_units, by_earnings, collections

    def _top_sellers(self, by_units: Iterable[TrendRecord], by_earnings: Iterable[TrendRecord]) -> dict[str, Any]:
        merged: dict[str, dict[str, Any]] = {}
        for source, badge, records in (("1A", "Top by Units", by_units), ("1B", "Top by Earnings", by_earnings)):
            for record in records:
                item = merged.setdefault(record.sku, self._item(record))
                item.setdefault("badges", [])
                if badge not in item["badges"]:
                    item["badges"].append(badge)
                item.setdefault("metadata", {})[source] = {"rank": record.rank}
        return {
            "slug": "top-sellers",
            "name": "Top Sellers",
            "description": "The products moving fastest by units and earnings.",
            "metadata": {"source": "combined_1A_1B", "dedupe": "sku"},
            "items": list(merged.values()),
        }

    def _item(self, record: TrendRecord) -> dict[str, Any]:
        return {
            "sku": record.sku,
            "item_count": record.item_count,
            "sale_amount": record.sale_amount,
            "total_earnings": record.total_earnings,
            "badges": [],
            "metadata": {"source_list_type": record.source_list_type, "rank": record.rank},
        }

    def _description_for(self, name: str, records: list[TrendRecord]) -> str:
        cats = sorted({r.category_list for r in records if r.category_list})
        if cats:
            return f"Curated Walmart picks across {', '.join(cats[:2])}."
        return f"Curated Walmart picks for {name}."

    def _weekly_collection_name(self, category: str) -> str:
        labels = {
            "Horticulture": "Spring Yard Refresh",
            "Sporting Goods": "Backyard Fun",
            "Home Management": "Home Helpers Trending Now",
            "Furniture": "Home Upgrade Finds",
            "Bedding": "Bedroom Refresh Picks",
            "Personal Care": "Everyday Personal Care Deals",
            "Pets & Supplies": "Pet Parent Restocks",
        }
        return labels.get(category, f"Trending {category}")


class ImpactPerformanceService:
    """Fetch and normalize latest 7-day Walmart SKU performance from Impact.

    Impact report configurations vary by account. The endpoint below is isolated
    so only this adapter needs adjustment if the configured report path differs.
    """

    BASE_URL = "https://api.impact.com/Mediapartners"

    def fetch_latest_7_days(self) -> tuple[str, str, list[TrendRecord]]:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=6)
        sid = os.environ.get("IMPACT_ACCOUNT_SID")
        token = os.environ.get("IMPACT_AUTH_TOKEN")
        if not sid or not token:
            raise RuntimeError("IMPACT_ACCOUNT_SID and IMPACT_AUTH_TOKEN are required for weekly refresh")

        endpoint = os.environ.get(
            "IMPACT_WALMART_PERFORMANCE_ENDPOINT",
            f"{self.BASE_URL}/{sid}/Reports/ProductPerformance",
        )
        params = {
            "StartDate": start.isoformat(),
            "EndDate": end.isoformat(),
            "CampaignId": os.environ.get("IMPACT_WALMART_CAMPAIGN_ID", "16662"),
            "PageSize": 10000,
        }
        logging.info(
            "[WALMART_TRENDS] Impact weekly fetch endpoint=%s window=%s..%s campaign_id=%s",
            endpoint, start.isoformat(), end.isoformat(), params["CampaignId"],
        )
        response = requests.get(endpoint, params=params, auth=(sid, token), timeout=30)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("Records") or payload.get("records") or payload.get("Actions") or payload.get("actions") or []
        records, stats = self._aggregate(rows)
        logging.info(
            "[WALMART_TRENDS] Impact weekly rows raw=%s with_sku=%s skipped=%s aggregated_skus=%s missing_performance_fields=%s",
            stats["raw_rows"], stats["rows_with_sku"], stats["skipped_rows"], len(records), stats["missing_performance_fields"],
        )
        if stats["missing_performance_fields"]:
            logging.warning(
                "[WALMART_TRENDS] Impact weekly response had %s rows missing item_count/sale_amount/earnings fields",
                stats["missing_performance_fields"],
            )
        return start.isoformat(), end.isoformat(), records

    def _aggregate(self, rows: list[dict[str, Any]]) -> tuple[list[TrendRecord], dict[str, int]]:
        grouped: dict[str, TrendRecord] = {}
        stats = {
            "raw_rows": len(rows),
            "rows_with_sku": 0,
            "skipped_rows": 0,
            "missing_performance_fields": 0,
        }
        for row in rows:
            sku = str(row.get("Sku") or row.get("SKU") or row.get("ItemId") or row.get("Item ID") or row.get("SubId2") or "").strip()
            if not sku or sku.lower() == "annual":
                stats["skipped_rows"] += 1
                continue
            item_count_raw = row.get("ItemCount") or row.get("Item Count") or row.get("Quantity") or row.get("Items")
            sale_amount_raw = row.get("SaleAmount") or row.get("Sale Amount") or row.get("Revenue") or row.get("Amount")
            earnings_raw = row.get("TotalEarnings") or row.get("Total Earnings") or row.get("Payout") or row.get("Commission")
            if item_count_raw is None and sale_amount_raw is None and earnings_raw is None:
                stats["missing_performance_fields"] += 1
            stats["rows_with_sku"] += 1
            rec = grouped.setdefault(sku, TrendRecord(
                sku=sku,
                item_name=str(row.get("ItemName") or row.get("Item Name") or row.get("Product") or ""),
                category_list=str(row.get("Category") or row.get("Category List") or ""),
                source_list_type="impact_weekly",
            ))
            rec.item_count += _to_int(item_count_raw)
            rec.sale_amount += _to_float(sale_amount_raw)
            rec.total_earnings += _to_float(earnings_raw)
            rec.category_list = rec.category_list or str(row.get("Category") or row.get("Category List") or "")
            rec.item_name = rec.item_name or str(row.get("ItemName") or row.get("Product") or "")
        return list(grouped.values()), stats


class WalmartTrendRefreshService:
    def __init__(self):
        db_schema.init_schema()
        self.store = WalmartTrendStore()
        self.builder = CollectionBuilder()
        self.enricher = WalmartProductEnricher(self.store)
        self.affiliates = AffiliateLinkService(self.store)
        self.urlgenius = URLGeniusLinkService(self.store)

    def bootstrap_from_workbook(self, workbook_path: str | os.PathLike[str] = DEFAULT_WORKBOOK) -> RefreshResult:
        file_meta = parse_workbook_filename(Path(workbook_path))
        run_id = self.store.create_run(
            "workbook_bootstrap",
            str(workbook_path),
            date_label=file_meta["date_label"],
        )
        try:
            parser = WorkbookTrendParser(workbook_path)
            parsed = parser.parse()
            diagnostics = parser.diagnostics(parsed)

            # Persist summary-tab metadata on the run record
            summary_meta = parsed.get("summary_meta") or {}
            if summary_meta:
                self.store.update_run_metadata(run_id, summary_meta)

            # Upsert all-SKUs product rows (brand, landing_page_url) and seed workbook links.
            # Handled before _process_records so enrichment can see the full inventory.
            seeded_links = 0
            for record in parsed.get("all_skus", []):
                try:
                    self.store.upsert_product_from_record(record)
                    if record.landing_page_url:
                        if self.store.seed_workbook_affiliate_link(record.sku, record.landing_page_url):
                            seeded_links += 1
                except Exception as exc:
                    logging.warning("[WALMART_TRENDS] all_skus upsert failed for %s: %s", record.sku, exc)
            if seeded_links:
                logging.info("[WALMART_TRENDS] Seeded %d workbook affiliate links", seeded_links)

            trend_records = parsed.get("1A", []) + parsed.get("1B", []) + parsed.get("collections", [])
            if not trend_records and not parsed.get("all_skus"):
                raise WorkbookValidationError("Workbook parsed successfully but produced no trend records")
            return self._process_records(
                run_id,
                "workbook_bootstrap",
                trend_records,
                self.builder.from_workbook(parsed),
                diagnostics=diagnostics,
            )
        except Exception as exc:
            failures = [{"stage": "workbook_parse", "error": str(exc)}]
            self.store.finish_run(run_id, "failed", {"records": 0}, failures)
            return RefreshResult(run_id, "failed", {"records": 0}, failures)

    def refresh_from_impact(self) -> RefreshResult:
        run_id = self.store.create_run("impact_weekly")
        failures: list[dict[str, str]] = []
        try:
            window_start, window_end, records = ImpactPerformanceService().fetch_latest_7_days()
            self._update_run_window(run_id, window_start, window_end)
            if not records:
                raise RuntimeError("Impact weekly refresh returned no SKU performance records")
            by_units, by_earnings, collections = self.builder.from_weekly_records(records)
            snapshot_records = records + by_units + by_earnings
            return self._process_records(run_id, "impact_weekly", snapshot_records, collections, window_start, window_end)
        except Exception as exc:
            failures.append({"stage": "impact_fetch", "error": str(exc)})
            self.store.finish_run(run_id, "failed", {"records": 0}, failures)
            return RefreshResult(run_id, "failed", {"records": 0}, failures)

    def _process_records(
        self,
        run_id: int,
        source_type: str,
        records: list[TrendRecord],
        collections: list[dict[str, Any]],
        window_start: str = "",
        window_end: str = "",
        diagnostics: dict[str, Any] | None = None,
    ) -> RefreshResult:
        failures: list[dict[str, str]] = []
        unique_skus = sorted({r.sku for r in records})
        for record in records:
            try:
                self.store.upsert_product_from_record(record)
                self.store.add_snapshot(run_id, source_type, record, window_start, window_end)
            except Exception as exc:
                failures.append({"stage": "snapshot", "sku": record.sku, "error": str(exc)})

        for sku in unique_skus:
            try:
                product = self.enricher.enrich(sku)
                product_url = product.get("canonical_url") or f"https://www.walmart.com/ip/{sku}"
                impact_url = self.affiliates.ensure(sku, product_url)
                self.urlgenius.ensure(impact_url, sku)
            except Exception as exc:
                failures.append({"stage": "link_or_enrich", "sku": sku, "error": str(exc)})

        collection_item_rows = sum(len(collection.get("items", [])) for collection in collections)
        try:
            self.store.replace_collections(run_id, source_type, collections)
        except Exception as exc:
            failures.append({"stage": "collections", "error": str(exc)})

        active_diagnostics = self.store.active_collection_diagnostics()
        counts = {
            "records": len(records),
            "unique_skus": len(unique_skus),
            "products_inserted_updated": len(unique_skus),
            "collections": len(collections),
            "collection_rows_inserted_updated": len(collections),
            "collection_item_rows_inserted": collection_item_rows,
            "active_collections": active_diagnostics.get("active_collection_count", 0),
            "failures": len(failures),
        }
        merged_diagnostics = {**(diagnostics or {}), **active_diagnostics}
        logging.info("[WALMART_TRENDS] refresh diagnostics: %s", json.dumps(merged_diagnostics, sort_keys=True))
        status = "partial" if failures else "success"
        self.store.finish_run(run_id, status, counts, failures)
        return RefreshResult(run_id, status, counts, failures, merged_diagnostics)

    def _update_run_window(self, run_id: int, window_start: str, window_end: str) -> None:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE walmart_refresh_runs SET window_start = ?, window_end = ? WHERE id = ?",
                (window_start, window_end, run_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_trending_page_data() -> dict[str, Any]:
    return WalmartTrendStore().landing_page_data()
