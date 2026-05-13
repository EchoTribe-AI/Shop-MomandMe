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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import db_schema
from product_api import CrawlbaseAPI, URLGeniusAPI
from utils.amazon_creators import (
    AmazonCreatorsAPI,
    AmazonCreatorsAPIError,
    AmazonCreatorsConfigError,
    AmazonCreatorsFatalError,
)
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

    def pending_asins_prioritized(self, limit: int = 30) -> list[str]:
        """ASINs needing enrichment, ordered by visibility priority.

        Priority: active collection items first, then any row missing
        image_url OR current_price, ordered by recency. Already-enriched
        rows with an image are skipped.
        """
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT p.asin
                FROM amazon_trend_products p
                LEFT JOIN (
                    SELECT DISTINCT ci.sku AS asin
                    FROM walmart_collection_items ci
                    JOIN walmart_collections c ON c.slug = ci.collection_slug
                    WHERE ci.retailer = 'amazon' AND c.is_active = 1
                ) active ON active.asin = p.asin
                WHERE COALESCE(p.enrichment_status, 'pending') != 'ok'
                   OR p.image_url IS NULL OR p.image_url = ''
                   OR p.current_price IS NULL
                ORDER BY (active.asin IS NOT NULL) DESC,
                         (p.image_url IS NULL OR p.image_url = '') DESC,
                         (p.current_price IS NULL) DESC,
                         p.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [r[0] for r in rows]
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
        """Return the best monetized URL for an ASIN.

        Preference order:
          1. An active or workbook-sourced row in amazon_affiliate_links.
          2. The Creators-vended detail_page_url on amazon_trend_products
             (kept verbatim per Amazon attribution rules).
        """
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
            if row and row[0]:
                return row[0]
            vended = conn.execute(
                "SELECT detail_page_url FROM amazon_trend_products WHERE asin = ?",
                (asin,),
            ).fetchone()
            return (vended[0] if vended and vended[0] else "") or ""
        finally:
            conn.close()

    def update_product_enrichment(
        self,
        asin: str,
        data: dict[str, Any],
        status: str = "ok",
        error: str = "",
    ) -> None:
        """Update display metadata on an existing amazon_trend_products row.

        Never overwrites a non-empty field with an empty value — callers pass
        whatever they have and COALESCE preserves the best-known value.
        Persists the Creators API fields (availability_type, availability_message,
        parent_asin, detail_page_url) when present.
        """
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE amazon_trend_products
                SET
                    image_url            = COALESCE(NULLIF(?, ''), image_url),
                    current_price        = COALESCE(?, current_price),
                    price_display        = COALESCE(NULLIF(?, ''), price_display),
                    brand                = COALESCE(NULLIF(?, ''), brand),
                    category             = COALESCE(NULLIF(?, ''), category),
                    availability_type    = COALESCE(NULLIF(?, ''), availability_type),
                    availability_message = COALESCE(NULLIF(?, ''), availability_message),
                    parent_asin          = COALESCE(NULLIF(?, ''), parent_asin),
                    detail_page_url      = COALESCE(NULLIF(?, ''), detail_page_url),
                    enrichment_status    = ?,
                    enrichment_error     = ?,
                    last_verified_at     = CURRENT_TIMESTAMP,
                    updated_at           = CURRENT_TIMESTAMP
                WHERE asin = ?
                """,
                (
                    data.get("image_url") or "",
                    data.get("current_price"),
                    data.get("price_display") or "",
                    data.get("brand") or "",
                    data.get("category") or "",
                    data.get("availability_type") or "",
                    data.get("availability_message") or "",
                    data.get("parent_asin") or "",
                    data.get("detail_page_url") or "",
                    status,
                    error,
                    asin,
                ),
            )
            conn.commit()
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


class AmazonURLGeniusLinkService:
    """Wrap Amazon affiliate URLs with URLGenius deep links.

    Stores in the shared walmart_urlgenius_links table (retailer-agnostic
    destination→genius_url cache). Never touches Walmart affiliate rows.
    """

    def __init__(self, store: AmazonTrendStore):
        self.store = store
        self.client = URLGeniusAPI()

    def ensure(self, affiliate_url: str, asin: str, force_new: bool = False) -> str:
        """Return a URLGenius short link for the Amazon affiliate URL.

        Creates one if not already cached. Returns the original URL on any
        failure so the caller always has a usable link.
        """
        if not affiliate_url:
            return affiliate_url
        existing = None if force_new else self.store.urlgenius_for(affiliate_url)
        if existing:
            return existing.get("genius_url") or affiliate_url
        if not self.client.api_key:
            self.store.save_urlgenius_link(
                affiliate_url, affiliate_url,
                status="fallback", error="URLGENIUS_API_KEY not set",
            )
            return affiliate_url
        try:
            result = self.client.create_link(
                affiliate_url,
                utm_source="amazon",
                utm_medium="affiliate",
                utm_campaign="trending-picks",
                utm_content=asin,
                force_new=force_new,
            )
            link = result.get("link", {}) if isinstance(result, dict) else {}
            genius_url = link.get("genius_url") or link.get("short_url") or affiliate_url
            self.store.save_urlgenius_link(affiliate_url, genius_url, str(link.get("id") or ""))
            return genius_url
        except Exception as exc:
            logging.warning("[AMAZON_TRENDS] URLGenius failed for %s: %s", asin, exc)
            self.store.save_urlgenius_link(
                affiliate_url, affiliate_url,
                status="fallback", error=str(exc),
            )
            return affiliate_url


class AmazonProductEnricher:
    """Fetch and store display metadata for Amazon products.

    Primary: Amazon Creators API GetItems (batched up to 10 ASINs/call).
    Fallback: Crawlbase JS-rendered PDP scrape, used only when Creators API
    is not configured or returns no usable data (no image AND no price) for
    a given ASIN.

    The vended `detailPageURL` from Creators is stored verbatim in
    `detail_page_url` — Amazon's docs warn that altering returned URL
    parameters can break affiliate attribution.
    """

    def __init__(
        self,
        store: AmazonTrendStore,
        creators: AmazonCreatorsAPI | None = None,
        fallback: CrawlbaseAPI | None = None,
    ):
        self.store = store
        try:
            self.creators = creators or AmazonCreatorsAPI()
        except AmazonCreatorsConfigError as exc:
            logging.warning("[AMAZON_TRENDS] Creators API config invalid: %s", exc)
            self.creators = None  # type: ignore[assignment]
        self.fallback = fallback or CrawlbaseAPI()
        # Back-compat alias — earlier tests/callers referenced `self.client`
        # when Crawlbase was the primary hydrator.
        self.client = self.fallback

    # ----- public API -----

    def enrich(self, asin: str) -> dict[str, Any]:
        """Enrich one ASIN. Routes through Creators first, then Crawlbase."""
        existing = self.store.get_product(asin) or {}
        if existing.get("enrichment_status") == "ok" and existing.get("image_url"):
            return existing
        # Try Creators API first.
        if self.creators and self.creators.configured:
            try:
                items = self.creators.get_items([asin])
            except AmazonCreatorsFatalError as exc:
                # Don't silently fall through to Crawlbase on a misconfig —
                # mark this ASIN so the operator sees the real reason.
                logging.error(
                    "[AMAZON_TRENDS] Creators API fatal for %s — reason=%s message=%s",
                    asin, exc.reason, exc.message,
                )
                self.store.update_product_enrichment(
                    asin, {}, "fallback", f"creators_fatal:{exc.reason}"
                )
                return existing
            except (AmazonCreatorsAPIError, AmazonCreatorsConfigError) as exc:
                logging.warning("[AMAZON_TRENDS] Creators API single-fetch failed for %s: %s", asin, exc)
                items = {}
            parsed = items.get(asin)
            if parsed and self._has_critical_fields(parsed):
                self.store.update_product_enrichment(asin, parsed, "ok")
                return {**existing, **{k: v for k, v in parsed.items() if v not in (None, "")}}
        # Fallback to Crawlbase.
        return self._enrich_via_crawlbase(asin, existing)

    def enrich_batch(self, asins: list[str], max_workers: int = 4) -> dict[str, int]:
        """Hydrate a list of ASINs.

        Primary path is Creators API GetItems in batches of 10. Any ASIN that
        is missing from the Creators response OR lacks critical fields (no
        image AND no price) is routed to Crawlbase concurrently.
        Returns counts {ok, pending, fallback, skipped, creators, crawlbase}.
        """
        counts: dict[str, int] = {
            "ok": 0, "pending": 0, "fallback": 0, "skipped": 0,
            "creators": 0, "crawlbase": 0,
        }
        to_run: list[str] = []
        for asin in asins:
            existing = self.store.get_product(asin) or {}
            if existing.get("enrichment_status") == "ok" and existing.get("image_url"):
                counts["skipped"] += 1
                continue
            to_run.append(asin)
        if not to_run:
            return counts
        logging.info(
            "[AMAZON_TRENDS] enrichment batch: %d ASIN(s), max_workers=%d",
            len(to_run), max_workers,
        )

        # Phase 1 — Creators API (primary).
        creators_results: dict[str, dict[str, Any]] = {}
        if self.creators and self.creators.configured:
            try:
                creators_results = self.creators.get_items(to_run)
            except AmazonCreatorsFatalError as exc:
                # Non-retryable misconfiguration (bad partnerTag, ineligible
                # associate, validation error). Stop the run rather than fall
                # back to Crawlbase for every ASIN — fixing the config is the
                # only correct action.
                logging.error(
                    "[AMAZON_TRENDS] Creators API fatal error — aborting enrichment run. "
                    "reason=%s status=%s message=%s",
                    exc.reason, exc.http_status, exc.message,
                )
                counts["fatal"] = counts.get("fatal", 0) + 1
                counts["fatal_reason"] = exc.reason  # type: ignore[assignment]
                return counts
            except AmazonCreatorsAPIError as exc:
                logging.warning("[AMAZON_TRENDS] Creators API batch failed: %s", exc)
                creators_results = {}
        else:
            logging.info(
                "[AMAZON_TRENDS] Creators API not configured — falling back to Crawlbase for all ASINs"
            )

        fallback_asins: list[str] = []
        for asin in to_run:
            parsed = creators_results.get(asin)
            if parsed and self._has_critical_fields(parsed):
                try:
                    self.store.update_product_enrichment(asin, parsed, "ok")
                    counts["creators"] += 1
                except Exception as exc:
                    logging.warning("[AMAZON_TRENDS] persist failed for %s: %s", asin, exc)
                    fallback_asins.append(asin)
            else:
                fallback_asins.append(asin)

        # Phase 2 — Crawlbase fallback (concurrent, isolated failures).
        if fallback_asins:
            logging.info(
                "[AMAZON_TRENDS] Crawlbase fallback for %d ASIN(s) missing critical fields",
                len(fallback_asins),
            )
            with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
                futures = {
                    pool.submit(self._enrich_via_crawlbase, asin, None): asin
                    for asin in fallback_asins
                }
                for fut in as_completed(futures):
                    asin = futures[fut]
                    try:
                        fut.result()
                    except Exception as exc:
                        logging.warning(
                            "[AMAZON_TRENDS] crawlbase worker failed for %s: %s", asin, exc
                        )
                    status = (self.store.get_product(asin) or {}).get("enrichment_status") or "pending"
                    if status == "ok":
                        counts["crawlbase"] += 1

        # Recompute final status counts from DB to keep them accurate.
        for asin in to_run:
            status = (self.store.get_product(asin) or {}).get("enrichment_status") or "pending"
            if status not in counts:
                counts[status] = 0
            counts[status] += 1
        return counts

    # ----- internals -----

    @staticmethod
    def _has_critical_fields(parsed: dict[str, Any]) -> bool:
        """A Creators item is usable if it has at least an image OR a price."""
        return bool(parsed.get("image_url")) or parsed.get("current_price") is not None

    def _enrich_via_crawlbase(
        self, asin: str, existing: dict[str, Any] | None
    ) -> dict[str, Any]:
        existing = existing if existing is not None else (self.store.get_product(asin) or {})
        if not self.fallback.token:
            # Neither primary nor fallback available; mark pending without write.
            self.store.update_product_enrichment(
                asin, {}, "pending", "Creators API unavailable and CRAWLBASE_JS_TOKEN not set"
            )
            return existing
        try:
            item = self.fallback.get_amazon_product(asin) or {}
            if not item:
                self.store.update_product_enrichment(asin, {}, "pending", "Crawlbase returned no data")
                return existing
            price_value = _to_float(item.get("current_price") or item.get("price"))
            data = {
                "image_url": item.get("image_url") or item.get("imageUrl") or item.get("image") or "",
                "current_price": price_value,
                "price_display": _price_display(price_value)
                    if price_value
                    else (item.get("price_display") or ""),
                "brand": item.get("brand") or "",
                "category": item.get("category") or "",
            }
            self.store.update_product_enrichment(asin, data, "ok")
            return {**existing, **{k: v for k, v in data.items() if v not in (None, "")}}
        except Exception as exc:
            logging.warning("[AMAZON_TRENDS] crawlbase fallback failed for %s: %s", asin, exc)
            self.store.update_product_enrichment(asin, {}, "fallback", str(exc))
            return existing


class AmazonTrendRefreshService:
    """Orchestrate a full Amazon workbook bootstrap."""

    def __init__(self) -> None:
        db_schema.bootstrap()
        self.store = AmazonTrendStore()
        self.builder = AmazonCollectionBuilder()
        self.urlgenius = AmazonURLGeniusLinkService(self.store)
        self.enricher = AmazonProductEnricher(self.store)

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

            result = self._process_records(
                run_id,
                trend_records,
                self.builder.from_workbook(parsed),
                diagnostics=diagnostics,
            )

            # Workbook import is intentionally NOT blocked on Crawlbase scraping.
            # New rows default to enrichment_status='pending' via the schema;
            # call enrich_pending(...) separately for a prioritized backfill.
            logging.info(
                "[AMAZON_TRENDS] bootstrap done; enrichment is decoupled. "
                "Run enrich_pending() to backfill image/price/brand."
            )

            return result
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

        seeded_genius = 0
        for record in records:
            if record.amazon_link:
                try:
                    self.store.seed_workbook_affiliate_link(record.asin, record.amazon_link)
                except Exception as exc:
                    logging.debug(
                        "[AMAZON_TRENDS] affiliate seed skipped for %s: %s", record.asin, exc
                    )
                try:
                    affiliate_url = self.store.affiliate_link_for(record.asin) or record.amazon_link
                    genius_url = self.urlgenius.ensure(affiliate_url, record.asin)
                    if genius_url and genius_url != affiliate_url:
                        seeded_genius += 1
                except Exception as exc:
                    logging.debug(
                        "[AMAZON_TRENDS] URLGenius skipped for %s: %s", record.asin, exc
                    )
        if seeded_genius:
            logging.info("[AMAZON_TRENDS] Created/cached %d URLGenius links", seeded_genius)

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
            "urlgenius_links": seeded_genius,
            "failures": len(failures),
        }
        logging.info(
            "[AMAZON_TRENDS] bootstrap diagnostics: %s",
            __import__("json").dumps(diagnostics or {}, sort_keys=True),
        )
        status = "partial" if failures else "success"
        self.store.finish_run(run_id, status, counts, failures)
        return RefreshResult(run_id, status, counts, failures, diagnostics or {})

    def enrich_pending(self, limit: int = 30, max_workers: int = 4) -> dict[str, int]:
        """Prioritized post-import enrichment pass.

        Selects ASINs that are missing image/price (or marked non-ok), prioritizing
        items in active collections. Runs concurrently with a small worker pool.
        Returns counts {ok, pending, fallback, skipped, queued}.
        """
        asins = self.store.pending_asins_prioritized(limit=limit)
        logging.info(
            "[AMAZON_TRENDS] enrich_pending: queued %d ASIN(s) (limit=%d, max_workers=%d)",
            len(asins), limit, max_workers,
        )
        if not asins:
            return {"ok": 0, "pending": 0, "fallback": 0, "skipped": 0, "queued": 0}
        counts = self.enricher.enrich_batch(asins, max_workers=max_workers)
        counts["queued"] = len(asins)
        logging.info("[AMAZON_TRENDS] enrich_pending done: %s", counts)
        return counts
