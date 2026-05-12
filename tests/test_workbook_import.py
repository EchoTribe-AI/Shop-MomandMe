"""Phase 1 + workbook discovery tests.

Covers:
- parse_workbook_filename
- discover_workbooks: scans directory, sorts newest-first, handles missing dir
- WorkbookTrendParser: all_skus + summary_meta returned from parse()
- _records_from_aggregated_sheet: brand + landing_page_url captured
- _parse_summary_tab: multi-pair row format parsed correctly
- WalmartTrendStore.seed_workbook_affiliate_link: never overwrites active/fallback
- WalmartTrendStore.upsert_product_from_record: brand persisted
- create_run: date_label stored
- landing_page_data: workbook-status link used as last resort
- Happy-path integration against real workbook file
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


REAL_WORKBOOK = Path("attached_assets/Walmart_May6th_Analysis.xlsx")


def _setup_db(db_path: str) -> None:
    """Bootstrap a fresh test DB."""
    os.environ["CACHE_DB_PATH"] = db_path
    import db_schema
    import walmart_trends

    db_schema.DB_PATH = db_path
    walmart_trends.DB_PATH = db_path

    conn = sqlite3.connect(db_path)
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
    conn.commit()
    conn.close()
    db_schema.bootstrap()


class TestParseWorkbookFilename(unittest.TestCase):
    def setUp(self):
        import walmart_trends
        self.wt = walmart_trends

    def test_walmart_standard(self):
        result = self.wt.parse_workbook_filename("Walmart_May12_Analysis.xlsx")
        self.assertEqual(result["source"], "Walmart")
        self.assertEqual(result["date_label"], "May12")

    def test_amazon_standard(self):
        result = self.wt.parse_workbook_filename("Amazon_Summer2025_Analysis.xlsx")
        self.assertEqual(result["source"], "Amazon")
        self.assertEqual(result["date_label"], "Summer2025")

    def test_unknown_prefix(self):
        result = self.wt.parse_workbook_filename("my_export.xlsx")
        self.assertEqual(result["source"], "unknown")
        self.assertEqual(result["date_label"], "")

    def test_no_analysis_suffix(self):
        result = self.wt.parse_workbook_filename("Walmart_Apr28.xlsx")
        self.assertEqual(result["source"], "Walmart")
        self.assertEqual(result["date_label"], "Apr28")

    def test_path_object(self):
        result = self.wt.parse_workbook_filename(Path("Walmart_Week1_Analysis.xlsx"))
        self.assertEqual(result["source"], "Walmart")
        self.assertEqual(result["date_label"], "Week1")


class TestRecordsFromAggregatedSheet(unittest.TestCase):
    def setUp(self):
        import walmart_trends
        self.parser = walmart_trends.WorkbookTrendParser.__new__(walmart_trends.WorkbookTrendParser)
        self.parser.workbook_path = Path("fake.xlsx")
        self.parser.sheet_names_found = []
        self.wt = walmart_trends

    def test_basic_row_without_optional_columns(self):
        rows = [{"SKU": "1234567890", "Item Name": "Red Mulch", "Category List": "Horticulture",
                 "Item Count": "25", "Sale Amount": "50.00", "Total Earnings": "4.30"}]
        records = self.parser._records_from_aggregated_sheet(rows)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.sku, "1234567890")
        self.assertEqual(r.item_name, "Red Mulch")
        self.assertEqual(r.category_list, "Horticulture")
        self.assertEqual(r.item_count, 25)
        self.assertAlmostEqual(r.sale_amount, 50.0)
        self.assertAlmostEqual(r.total_earnings, 4.30)
        self.assertEqual(r.source_list_type, "all_skus")
        self.assertEqual(r.brand, "")
        self.assertEqual(r.landing_page_url, "")

    def test_brand_and_landing_page_url_captured(self):
        rows = [{"SKU": "9998887770", "Item Name": "Blue Mulch", "Category List": "Horticulture",
                 "Item Count": "10", "Sale Amount": "20.00", "Total Earnings": "2.00",
                 "Brand": "Scotts", "Landing Page URL": "https://www.walmart.com/ip/blue-mulch/9998887770"}]
        records = self.parser._records_from_aggregated_sheet(rows)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].brand, "Scotts")
        self.assertEqual(records[0].landing_page_url, "https://www.walmart.com/ip/blue-mulch/9998887770")

    def test_skips_rows_with_no_sku(self):
        rows = [
            {"SKU": "", "Item Name": "Mystery Product", "Category List": "x"},
            {"SKU": "111", "Item Name": "Real Product", "Category List": "y"},
        ]
        records = self.parser._records_from_aggregated_sheet(rows)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].sku, "111")


class TestParseSummaryTab(unittest.TestCase):
    def setUp(self):
        import walmart_trends
        self.parser = walmart_trends.WorkbookTrendParser.__new__(walmart_trends.WorkbookTrendParser)

    def test_empty_rows_returns_empty_dict(self):
        self.assertEqual(self.parser._parse_summary_tab([]), {})

    def test_simple_kv_pair(self):
        rows = [{"col0": "Source file", "col1": "Walmart 14 days.csv", "col2": ""}]
        meta = self.parser._parse_summary_tab(rows)
        self.assertEqual(meta.get("source_file"), "Walmart 14 days.csv")

    def test_two_pairs_per_row(self):
        rows = [{"col0": "Rows in export", "col1": "6183", "col2": "",
                 "col3": "Unique SKUs", "col4": "5788", "col5": ""}]
        meta = self.parser._parse_summary_tab(rows)
        self.assertEqual(meta.get("rows_in_export"), "6183")
        self.assertEqual(meta.get("unique_skus"), "5788")

    def test_title_row_skipped(self):
        # A title-only row where value cell is empty should not produce garbage keys
        rows = [{"col0": "Walmart 14-Day Product Performance Analysis", "col1": ""}]
        meta = self.parser._parse_summary_tab(rows)
        self.assertNotIn("walmart_14-day_product_performance_analysis", meta)

    def test_note_field_captured(self):
        rows = [{"col0": "Note", "col1": "Non-product rows excluded"}]
        meta = self.parser._parse_summary_tab(rows)
        self.assertEqual(meta.get("note"), "Non-product rows excluded")


class TestSeedWorkbookAffiliateLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        self.store = walmart_trends.WalmartTrendStore()
        # Seed a product so FK-like references work
        self.store.upsert_product_from_record(walmart_trends.TrendRecord(sku="SKU001", item_name="Test"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_seeds_when_no_existing_link(self):
        seeded = self.store.seed_workbook_affiliate_link("SKU001", "https://www.walmart.com/ip/test/SKU001")
        self.assertTrue(seeded)
        # Verify the row exists with status='workbook'
        import walmart_trends
        conn = walmart_trends._connect()
        row = conn.execute(
            "SELECT status FROM walmart_affiliate_links WHERE sku = ?", ("SKU001",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "workbook")

    def test_skips_when_active_link_exists(self):
        self.store.save_affiliate_link("SKU001", "https://walmart.com/ip/SKU001", "https://impact.com/SKU001", "active")
        seeded = self.store.seed_workbook_affiliate_link("SKU001", "https://www.walmart.com/ip/test/SKU001")
        self.assertFalse(seeded)

    def test_skips_when_fallback_link_exists(self):
        self.store.save_affiliate_link("SKU001", "https://walmart.com/ip/SKU001", "https://impact.com/SKU001", "fallback")
        seeded = self.store.seed_workbook_affiliate_link("SKU001", "https://www.walmart.com/ip/test/SKU001")
        self.assertFalse(seeded)

    def test_blank_url_returns_false(self):
        self.assertFalse(self.store.seed_workbook_affiliate_link("SKU001", ""))

    def test_blank_sku_returns_false(self):
        self.assertFalse(self.store.seed_workbook_affiliate_link("", "https://walmart.com/ip/x"))


class TestBrandPersistedFromRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        self.store = walmart_trends.WalmartTrendStore()
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def test_brand_written_on_insert(self):
        record = self.wt.TrendRecord(sku="BSKU1", item_name="Blue Mulch", brand="Scotts")
        self.store.upsert_product_from_record(record)
        product = self.store.get_product("BSKU1")
        self.assertEqual(product["brand"], "Scotts")

    def test_brand_not_overwritten_by_empty(self):
        self.store.upsert_product_from_record(self.wt.TrendRecord(sku="BSKU2", brand="Miracle-Gro"))
        self.store.upsert_product_from_record(self.wt.TrendRecord(sku="BSKU2", brand=""))
        product = self.store.get_product("BSKU2")
        self.assertEqual(product["brand"], "Miracle-Gro")

    def test_brand_updated_when_better_value_available(self):
        self.store.upsert_product_from_record(self.wt.TrendRecord(sku="BSKU3", brand=""))
        self.store.upsert_product_from_record(self.wt.TrendRecord(sku="BSKU3", brand="Scotts"))
        product = self.store.get_product("BSKU3")
        self.assertEqual(product["brand"], "Scotts")


class TestDateLabelStoredOnRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        self.store = walmart_trends.WalmartTrendStore()
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def test_date_label_stored(self):
        run_id = self.store.create_run("workbook_bootstrap", "Walmart_May12_Analysis.xlsx", date_label="May12")
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT date_label FROM walmart_refresh_runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
        self.assertEqual(row["date_label"], "May12")

    def test_empty_date_label_stored_as_null(self):
        run_id = self.store.create_run("impact_weekly")
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT date_label FROM walmart_refresh_runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
        self.assertIsNone(row["date_label"])


class TestWorkbookLinkUsedInLandingPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        import db_schema
        self.store = walmart_trends.WalmartTrendStore()
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_collection_with_sku(self, sku: str) -> None:
        """Seed a minimal walmart_collections + walmart_collection_items + walmart_products row."""
        conn = self.wt._connect()
        conn.execute(
            "INSERT OR IGNORE INTO walmart_products (sku, item_name) VALUES (?, ?)",
            (sku, f"Product {sku}"),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collections
            (slug, name, description, source_type, is_active, display_order, metadata_json)
            VALUES ('test-col', 'Test Collection', '', 'workbook_bootstrap', 1, 0, '{}')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collection_items
            (collection_slug, sku, display_order, badges_json)
            VALUES ('test-col', ?, 0, '[]')
            """,
            (sku,),
        )
        conn.commit()
        conn.close()

    def test_workbook_link_returned_when_no_impact_link(self):
        sku = "WB001"
        self._seed_collection_with_sku(sku)
        workbook_url = "https://www.walmart.com/ip/some-product/WB001"
        self.store.seed_workbook_affiliate_link(sku, workbook_url)

        data = self.store.landing_page_data()
        collection = next((c for c in data["collections"] if c["slug"] == "test-col"), None)
        self.assertIsNotNone(collection)
        item = next((i for i in collection["items"] if i["sku"] == sku), None)
        self.assertIsNotNone(item)
        self.assertEqual(item["shop_url"], workbook_url)

    def test_active_link_takes_priority_over_workbook(self):
        sku = "WB002"
        self._seed_collection_with_sku(sku)
        self.store.seed_workbook_affiliate_link(sku, "https://www.walmart.com/ip/WB002")
        # Now add an active Impact link
        self.store.save_affiliate_link(sku, "https://walmart.com/ip/WB002", "https://impact.go/WB002", "active")

        data = self.store.landing_page_data()
        collection = next((c for c in data["collections"] if c["slug"] == "test-col"), None)
        item = next((i for i in collection["items"] if i["sku"] == sku), None)
        # shop_url should use the Impact URL, not the workbook URL
        self.assertIn("impact.go", item["shop_url"])


@unittest.skipUnless(REAL_WORKBOOK.exists(), "Real workbook not present")
class TestRealWorkbookIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_includes_all_skus(self):
        parsed = self.wt.WorkbookTrendParser(REAL_WORKBOOK).parse()
        self.assertIn("all_skus", parsed)
        self.assertGreater(len(parsed["all_skus"]), 100)

    def test_parse_includes_summary_meta(self):
        parsed = self.wt.WorkbookTrendParser(REAL_WORKBOOK).parse()
        meta = parsed.get("summary_meta") or {}
        self.assertIsInstance(meta, dict)
        # Should have extracted at least source_file and unique_skus
        self.assertIn("source_file", meta)
        self.assertIn("unique_skus", meta)

    def test_parse_workbook_filename_for_real_workbook(self):
        result = self.wt.parse_workbook_filename(REAL_WORKBOOK)
        self.assertEqual(result["source"], "Walmart")
        self.assertNotEqual(result["date_label"], "")

    def test_1a_1b_still_ten_records(self):
        parsed = self.wt.WorkbookTrendParser(REAL_WORKBOOK).parse()
        self.assertEqual(len(parsed["1A"]), 10)
        self.assertEqual(len(parsed["1B"]), 10)

    def test_diagnostics_includes_new_fields(self):
        parser = self.wt.WorkbookTrendParser(REAL_WORKBOOK)
        parsed = parser.parse()
        diag = parser.diagnostics(parsed)
        self.assertIn("all_skus_count", diag)
        self.assertGreater(diag["all_skus_count"], 100)
        self.assertEqual(diag["date_label"], "May6th")
        self.assertEqual(diag["source"], "Walmart")


class TestDiscoverWorkbooks(unittest.TestCase):
    def setUp(self):
        import walmart_trends
        self.wt = walmart_trends
        self.tmp = tempfile.TemporaryDirectory()
        self.assets = Path(self.tmp.name) / "assets"
        self.assets.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name: str, mtime_offset: float = 0) -> Path:
        p = self.assets / name
        p.write_bytes(b"fake")
        import time
        t = time.time() + mtime_offset
        import os
        os.utime(p, (t, t))
        return p

    def test_empty_dir_returns_empty_list(self):
        result = self.wt.discover_workbooks(self.assets)
        self.assertEqual(result, [])

    def test_missing_dir_returns_empty_list(self):
        result = self.wt.discover_workbooks(Path(self.tmp.name) / "nonexistent")
        self.assertEqual(result, [])

    def test_non_xlsx_files_excluded(self):
        self._touch("report.csv")
        self._touch("notes.txt")
        result = self.wt.discover_workbooks(self.assets)
        self.assertEqual(result, [])

    def test_xlsx_file_included_with_metadata(self):
        self._touch("Walmart_May12_Analysis.xlsx")
        result = self.wt.discover_workbooks(self.assets)
        self.assertEqual(len(result), 1)
        r = result[0]
        self.assertEqual(r["filename"], "Walmart_May12_Analysis.xlsx")
        self.assertEqual(r["source"], "Walmart")
        self.assertEqual(r["date_label"], "May12")
        self.assertIn("modified_at", r)
        self.assertIn("modified_display", r)
        self.assertTrue(r["path"].endswith("Walmart_May12_Analysis.xlsx"))

    def test_sorted_newest_first(self):
        # older file has mtime_offset=0, newer has +10s
        self._touch("Walmart_Apr28_Analysis.xlsx", mtime_offset=0)
        import time; time.sleep(0.02)
        self._touch("Walmart_May12_Analysis.xlsx", mtime_offset=10)
        result = self.wt.discover_workbooks(self.assets)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["date_label"], "May12")
        self.assertEqual(result[1]["date_label"], "Apr28")

    def test_unknown_source_still_included(self):
        self._touch("my_export.xlsx")
        result = self.wt.discover_workbooks(self.assets)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "unknown")
        self.assertEqual(result[0]["date_label"], "")

    def test_amazon_source_detected(self):
        self._touch("Amazon_Summer2025_Analysis.xlsx")
        result = self.wt.discover_workbooks(self.assets)
        self.assertEqual(result[0]["source"], "Amazon")
        self.assertEqual(result[0]["date_label"], "Summer2025")


@unittest.skipUnless(REAL_WORKBOOK.exists(), "Real workbook not present")
class TestDiscoverWorkbooksWithRealFile(unittest.TestCase):
    def test_real_workbook_discovered(self):
        import walmart_trends
        results = walmart_trends.discover_workbooks()
        paths = [r["path"] for r in results]
        self.assertTrue(any("Walmart_May6th" in p for p in paths))
        first = results[0]
        self.assertIn("modified_at", first)
        self.assertIn("modified_display", first)


if __name__ == "__main__":
    unittest.main()
