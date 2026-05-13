"""Phase 2 tests — Amazon ingestion + unified data model.

Covers:
- _ensure_amazon_tag: append-only when no tag present, no double-tagging
- AmazonWorkbookParser: validation, record parsing from sheet rows
- AmazonTrendStore: upsert_product, add_snapshot, seed_workbook_affiliate_link,
  affiliate_link_for, replace_collections delegation
- AmazonCollectionBuilder: from_workbook collections structure
- replace_collections retailer scoping: Walmart collections not wiped by Amazon run
- landing_page_data retailer-aware dispatch: Walmart and Amazon items in same response
- Chat non-breaking smoke test: mixed-retailer collections don't crash storefront_chat_sessions
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _setup_db(db_path: str) -> None:
    """Bootstrap a fresh test DB (mirrors test_workbook_import._setup_db)."""
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


# ---------------------------------------------------------------------------
# _ensure_amazon_tag
# ---------------------------------------------------------------------------

class TestEnsureAmazonTag(unittest.TestCase):
    def setUp(self):
        import amazon_trends
        self.at = amazon_trends

    def test_tag_appended_to_clean_url(self):
        url = "https://www.amazon.com/dp/B08N5KWB9H"
        result = self.at._ensure_amazon_tag(url, tag="test-tag-20")
        self.assertIn("tag=test-tag-20", result)

    def test_tag_not_doubled_when_already_present(self):
        url = "https://www.amazon.com/dp/B08N5KWB9H?tag=existing-20"
        result = self.at._ensure_amazon_tag(url, tag="test-tag-20")
        self.assertEqual(result, url)
        self.assertEqual(result.count("tag="), 1)

    def test_tag_appended_to_url_with_other_params(self):
        url = "https://www.amazon.com/dp/B08N5KWB9H?ref=sr_1_1"
        result = self.at._ensure_amazon_tag(url, tag="test-tag-20")
        self.assertIn("tag=test-tag-20", result)
        self.assertIn("ref=sr_1_1", result)

    def test_empty_url_returned_unchanged(self):
        self.assertEqual(self.at._ensure_amazon_tag(""), "")

    def test_empty_tag_returns_url_unchanged(self):
        url = "https://www.amazon.com/dp/B08N5KWB9H"
        with patch.object(self.at, "AMAZON_AFFILIATE_TAG", ""):
            result = self.at._ensure_amazon_tag(url, tag="")
        self.assertEqual(result, url)

    def test_uses_module_default_tag_when_no_tag_arg(self):
        url = "https://www.amazon.com/dp/B08N5KWB9H"
        result = self.at._ensure_amazon_tag(url)
        self.assertIn(f"tag={self.at.AMAZON_AFFILIATE_TAG}", result)


# ---------------------------------------------------------------------------
# AmazonWorkbookParser (unit — no real file needed)
# ---------------------------------------------------------------------------

class TestAmazonWorkbookParserRecords(unittest.TestCase):
    def setUp(self):
        import amazon_trends
        self.parser = amazon_trends.AmazonWorkbookParser.__new__(amazon_trends.AmazonWorkbookParser)
        self.parser.workbook_path = Path("fake_amazon.xlsx")
        self.parser.sheet_names_found = []
        self.at = amazon_trends

    def test_basic_asin_row_parsed(self):
        rows = [{"ASIN": "B08N5KWB9H", "Product Title": "Echo Dot", "Clicks": "120",
                 "Items Ordered": "10", "Items Shipped": "9", "Items Returned": "1",
                 "Items Shipped Revenue": "99.00", "Items Shipped Earnings": "4.50",
                 "Total Earnings": "5.20"}]
        records = self.parser._amazon_records_from_sheet(rows, "2A")
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.asin, "B08N5KWB9H")
        self.assertEqual(r.product_title, "Echo Dot")
        self.assertEqual(r.clicks, 120)
        self.assertEqual(r.items_shipped, 9)
        self.assertAlmostEqual(r.total_earnings, 5.20)
        self.assertEqual(r.source_list_type, "2A")
        self.assertEqual(r.rank, 1)

    def test_skips_rows_without_asin(self):
        rows = [
            {"ASIN": "", "Product Title": "Ghost", "Clicks": "0", "Total Earnings": "0"},
            {"ASIN": "B000VALID", "Product Title": "Real", "Clicks": "5", "Total Earnings": "1"},
        ]
        records = self.parser._amazon_records_from_sheet(rows, "2B")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].asin, "B000VALID")

    def test_amazon_tag_appended_to_link(self):
        rows = [{"ASIN": "B0001", "Product Title": "Thing",
                 "Amazon Link": "https://www.amazon.com/dp/B0001",
                 "Clicks": "1", "Total Earnings": "0.10"}]
        records = self.parser._amazon_records_from_sheet(rows, "2A")
        self.assertIn("tag=", records[0].amazon_link)

    def test_collection_name_captured(self):
        rows = [{"ASIN": "B0002", "Product Title": "Gadget", "Collection": "Tech Picks",
                 "Clicks": "50", "Total Earnings": "2.00"}]
        records = self.parser._amazon_records_from_sheet(rows, "collection")
        self.assertEqual(records[0].collection_name, "Tech Picks")

    def test_rank_increments_per_row(self):
        rows = [
            {"ASIN": "B001", "Product Title": "A", "Clicks": "5", "Total Earnings": "1"},
            {"ASIN": "B002", "Product Title": "B", "Clicks": "3", "Total Earnings": "0.5"},
        ]
        records = self.parser._amazon_records_from_sheet(rows, "2C")
        self.assertEqual(records[0].rank, 1)
        self.assertEqual(records[1].rank, 2)

    def test_validate_raises_on_missing_required_sheet(self):
        from amazon_trends import AmazonWorkbookParser
        from walmart_trends import WorkbookValidationError
        rows_by_sheet = {
            "Trending - Earnings First": [{"ASIN": "X", "Product Title": "Y", "Clicks": "1", "Total Earnings": "0"}],
            "Trending - Items Shipped First": [{"ASIN": "X", "Product Title": "Y", "Items Shipped": "1", "Total Earnings": "0"}],
            "Curated Collections": [{"Collection": "C", "ASIN": "X", "Product Title": "Y"}],
            # "Trending - Clicks First" intentionally missing
        }
        parser = AmazonWorkbookParser.__new__(AmazonWorkbookParser)
        parser.workbook_path = Path("fake.xlsx")
        parser.sheet_names_found = list(rows_by_sheet.keys())
        with self.assertRaises(WorkbookValidationError):
            parser._validate(rows_by_sheet)


# ---------------------------------------------------------------------------
# AmazonTrendStore
# ---------------------------------------------------------------------------

class TestAmazonTrendStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.store = amazon_trends.AmazonTrendStore()
        self.at = amazon_trends

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_product_inserts(self):
        record = self.at.AmazonTrendRecord(asin="B001TEST", product_title="Test Widget")
        self.store.upsert_product(record)
        product = self.store.get_product("B001TEST")
        self.assertIsNotNone(product)
        self.assertEqual(product["product_title"], "Test Widget")

    def test_upsert_product_does_not_overwrite_title_with_empty(self):
        self.store.upsert_product(self.at.AmazonTrendRecord(asin="B002", product_title="Keep Me"))
        self.store.upsert_product(self.at.AmazonTrendRecord(asin="B002", product_title=""))
        self.assertEqual(self.store.get_product("B002")["product_title"], "Keep Me")

    def test_add_snapshot_inserts_row(self):
        record = self.at.AmazonTrendRecord(
            asin="B003", source_list_type="2A", rank=1, clicks=50, total_earnings=3.50
        )
        run_id = self.store.create_run("fake_workbook.xlsx", date_label="May12")
        self.store.add_snapshot(run_id, record)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM amazon_product_performance_snapshots WHERE asin = ?", ("B003",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["clicks"], 50)
        self.assertAlmostEqual(row["total_earnings"], 3.50)

    def test_seed_workbook_affiliate_link_inserts_once(self):
        inserted = self.store.seed_workbook_affiliate_link(
            "B004", "https://www.amazon.com/dp/B004?tag=test-20"
        )
        self.assertTrue(inserted)
        # Second call must not overwrite
        inserted2 = self.store.seed_workbook_affiliate_link(
            "B004", "https://www.amazon.com/dp/B004?tag=other-20"
        )
        self.assertFalse(inserted2)

    def test_seed_workbook_affiliate_link_blank_inputs(self):
        self.assertFalse(self.store.seed_workbook_affiliate_link("", "https://amazon.com/dp/X"))
        self.assertFalse(self.store.seed_workbook_affiliate_link("B005", ""))

    def test_affiliate_link_for_returns_stored_link(self):
        self.store.seed_workbook_affiliate_link("B006", "https://amazon.com/dp/B006?tag=t-20")
        link = self.store.affiliate_link_for("B006")
        self.assertEqual(link, "https://amazon.com/dp/B006?tag=t-20")

    def test_affiliate_link_for_missing_asin_returns_empty(self):
        self.assertEqual(self.store.affiliate_link_for("NOTEXIST"), "")

    def test_create_run_stores_source_type_amazon(self):
        run_id = self.store.create_run("Amazon_May12_Analysis.xlsx", date_label="May12")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_type, date_label FROM walmart_refresh_runs WHERE id = ?", (run_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row["source_type"], "amazon_workbook_bootstrap")
        self.assertEqual(row["date_label"], "May12")


# ---------------------------------------------------------------------------
# replace_collections retailer scoping
# ---------------------------------------------------------------------------

class TestReplaceCollectionsRetailerScoping(unittest.TestCase):
    """Ensure Walmart collections survive an Amazon replace_collections run and vice versa."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        self.wt_store = walmart_trends.WalmartTrendStore()
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_walmart_collection(self, run_id: int) -> None:
        self.wt_store.replace_collections(
            run_id,
            "workbook_bootstrap",
            [{"slug": "wmt-col", "name": "Walmart Finds", "items": []}],
            retailer="walmart",
        )

    def _seed_amazon_collection(self, run_id: int) -> None:
        self.wt_store.replace_collections(
            run_id,
            "amazon_workbook_bootstrap",
            [{"slug": "amz-col", "name": "Amazon Picks", "items": []}],
            retailer="amazon",
        )

    def test_amazon_run_does_not_deactivate_walmart_collections(self):
        wmt_run = self.wt_store.create_run("workbook_bootstrap", "Walmart_May12.xlsx")
        self.wt_store.finish_run(wmt_run, "success", {}, [])
        self._seed_walmart_collection(wmt_run)

        amz_run = self.wt_store.create_run("amazon_workbook_bootstrap", "Amazon_May12.xlsx")
        self.wt_store.finish_run(amz_run, "success", {}, [])
        self._seed_amazon_collection(amz_run)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        wmt = conn.execute(
            "SELECT is_active FROM walmart_collections WHERE slug = 'wmt-col'"
        ).fetchone()
        amz = conn.execute(
            "SELECT is_active FROM walmart_collections WHERE slug = 'amz-col'"
        ).fetchone()
        conn.close()

        self.assertEqual(wmt["is_active"], 1, "Walmart collection should remain active")
        self.assertEqual(amz["is_active"], 1, "Amazon collection should be active")

    def test_walmart_run_does_not_deactivate_amazon_collections(self):
        amz_run = self.wt_store.create_run("amazon_workbook_bootstrap", "Amazon_May12.xlsx")
        self.wt_store.finish_run(amz_run, "success", {}, [])
        self._seed_amazon_collection(amz_run)

        wmt_run = self.wt_store.create_run("workbook_bootstrap", "Walmart_May12.xlsx")
        self.wt_store.finish_run(wmt_run, "success", {}, [])
        self._seed_walmart_collection(wmt_run)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        amz = conn.execute(
            "SELECT is_active FROM walmart_collections WHERE slug = 'amz-col'"
        ).fetchone()
        conn.close()
        self.assertEqual(amz["is_active"], 1, "Amazon collection should survive Walmart run")

    def test_second_walmart_run_deactivates_first_walmart_collection(self):
        run1 = self.wt_store.create_run("workbook_bootstrap", "Walmart_A.xlsx")
        self.wt_store.finish_run(run1, "success", {}, [])
        self._seed_walmart_collection(run1)

        run2 = self.wt_store.create_run("workbook_bootstrap", "Walmart_B.xlsx")
        self.wt_store.finish_run(run2, "success", {}, [])
        self.wt_store.replace_collections(
            run2, "workbook_bootstrap",
            [{"slug": "wmt-col-v2", "name": "Walmart Finds V2", "items": []}],
            retailer="walmart",
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        old = conn.execute(
            "SELECT is_active FROM walmart_collections WHERE slug = 'wmt-col'"
        ).fetchone()
        conn.close()
        self.assertEqual(old["is_active"], 0, "Old Walmart collection should be deactivated")

    def test_retailer_column_written_on_insert(self):
        run_id = self.wt_store.create_run("amazon_workbook_bootstrap", "Amazon_May12.xlsx")
        self.wt_store.finish_run(run_id, "success", {}, [])
        self._seed_amazon_collection(run_id)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT retailer FROM walmart_collections WHERE slug = 'amz-col'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["retailer"], "amazon")


# ---------------------------------------------------------------------------
# landing_page_data retailer-aware dispatch
# ---------------------------------------------------------------------------

class TestLandingPageDataRetailerAware(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        self.store = walmart_trends.WalmartTrendStore()
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_walmart_collection_with_item(self) -> None:
        conn = self.wt._connect()
        conn.execute(
            "INSERT OR IGNORE INTO walmart_products (sku, item_name) VALUES ('WMT001', 'Walmart Widget')"
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collections
            (slug, name, source_type, is_active, display_order, metadata_json, retailer)
            VALUES ('wmt-col', 'Walmart Finds', 'workbook_bootstrap', 1, 1, '{}', 'walmart')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collection_items
            (collection_slug, sku, display_order, badges_json, retailer)
            VALUES ('wmt-col', 'WMT001', 0, '[]', 'walmart')
            """
        )
        conn.commit()
        conn.close()

    def _seed_amazon_collection_with_item(self) -> None:
        conn = self.wt._connect()
        conn.execute(
            "INSERT OR IGNORE INTO amazon_trend_products (asin, product_title) VALUES ('B001AMAZON', 'Amazon Gadget')"
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collections
            (slug, name, source_type, is_active, display_order, metadata_json, retailer)
            VALUES ('amz-col', 'Amazon Picks', 'amazon_workbook_bootstrap', 1, 2, '{}', 'amazon')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collection_items
            (collection_slug, sku, display_order, badges_json, retailer)
            VALUES ('amz-col', 'B001AMAZON', 0, '[]', 'amazon')
            """
        )
        conn.commit()
        conn.close()

    def test_walmart_collection_has_retailer_walmart(self):
        self._seed_walmart_collection_with_item()
        data = self.store.landing_page_data()
        wmt_col = next((c for c in data["collections"] if c["slug"] == "wmt-col"), None)
        self.assertIsNotNone(wmt_col)
        self.assertEqual(wmt_col["retailer"], "walmart")

    def test_amazon_collection_has_retailer_amazon(self):
        self._seed_amazon_collection_with_item()
        data = self.store.landing_page_data()
        amz_col = next((c for c in data["collections"] if c["slug"] == "amz-col"), None)
        self.assertIsNotNone(amz_col)
        self.assertEqual(amz_col["retailer"], "amazon")

    def test_walmart_item_has_retailer_walmart(self):
        self._seed_walmart_collection_with_item()
        data = self.store.landing_page_data()
        wmt_col = next((c for c in data["collections"] if c["slug"] == "wmt-col"), None)
        self.assertEqual(len(wmt_col["items"]), 1)
        self.assertEqual(wmt_col["items"][0]["retailer"], "walmart")

    def test_amazon_item_has_retailer_amazon(self):
        self._seed_amazon_collection_with_item()
        data = self.store.landing_page_data()
        amz_col = next((c for c in data["collections"] if c["slug"] == "amz-col"), None)
        self.assertEqual(len(amz_col["items"]), 1)
        item = amz_col["items"][0]
        self.assertEqual(item["retailer"], "amazon")
        self.assertEqual(item["sku"], "B001AMAZON")
        self.assertEqual(item["title"], "Amazon Gadget")

    def test_amazon_item_shop_url_falls_back_to_amazon_dp(self):
        self._seed_amazon_collection_with_item()
        data = self.store.landing_page_data()
        amz_col = next((c for c in data["collections"] if c["slug"] == "amz-col"), None)
        item = amz_col["items"][0]
        self.assertIn("amazon.com/dp/B001AMAZON", item["shop_url"])

    def test_amazon_item_uses_affiliate_url_when_present(self):
        self._seed_amazon_collection_with_item()
        conn = self.wt._connect()
        conn.execute(
            """
            INSERT INTO amazon_affiliate_links (asin, product_url, affiliate_url, status)
            VALUES ('B001AMAZON', 'https://amazon.com/dp/B001AMAZON', 'https://amazon.com/dp/B001AMAZON?tag=test-20', 'workbook')
            """
        )
        conn.commit()
        conn.close()
        data = self.store.landing_page_data()
        amz_col = next((c for c in data["collections"] if c["slug"] == "amz-col"), None)
        self.assertIn("tag=test-20", amz_col["items"][0]["shop_url"])

    def test_mixed_retailer_collections_both_returned(self):
        self._seed_walmart_collection_with_item()
        self._seed_amazon_collection_with_item()
        data = self.store.landing_page_data()
        slugs = {c["slug"] for c in data["collections"]}
        self.assertIn("wmt-col", slugs)
        self.assertIn("amz-col", slugs)

    def test_walmart_item_missing_product_row_excluded(self):
        """Items without a matching product row should be dropped silently."""
        conn = self.wt._connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collections
            (slug, name, source_type, is_active, display_order, metadata_json, retailer)
            VALUES ('ghost-col', 'Ghost', 'workbook_bootstrap', 1, 3, '{}', 'walmart')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO walmart_collection_items
            (collection_slug, sku, display_order, badges_json, retailer)
            VALUES ('ghost-col', 'NOEXIST', 0, '[]', 'walmart')
            """
        )
        conn.commit()
        conn.close()
        data = self.store.landing_page_data()
        ghost_col = next((c for c in data["collections"] if c["slug"] == "ghost-col"), None)
        self.assertIsNotNone(ghost_col)
        self.assertEqual(len(ghost_col["items"]), 0)


# ---------------------------------------------------------------------------
# AmazonCollectionBuilder
# ---------------------------------------------------------------------------

class TestAmazonCollectionBuilder(unittest.TestCase):
    def setUp(self):
        import amazon_trends
        self.builder = amazon_trends.AmazonCollectionBuilder()
        self.at = amazon_trends

    def _make_record(self, asin: str, source: str, rank: int = 1, earnings: float = 1.0):
        return self.at.AmazonTrendRecord(
            asin=asin, source_list_type=source, rank=rank, total_earnings=earnings
        )

    def test_top_picks_collection_created(self):
        parsed = {
            "2A": [self._make_record("B001", "2A", rank=1)],
            "2B": [self._make_record("B002", "2B", rank=1)],
            "2C": [self._make_record("B003", "2C", rank=1)],
            "collections": [],
        }
        collections = self.builder.from_workbook(parsed)
        slugs = [c["slug"] for c in collections]
        self.assertIn("amazon-top-picks", slugs)

    def test_top_picks_dedupes_asin_across_lists(self):
        parsed = {
            "2A": [self._make_record("B001", "2A", rank=1)],
            "2B": [self._make_record("B001", "2B", rank=1)],
            "2C": [],
            "collections": [],
        }
        collections = self.builder.from_workbook(parsed)
        top = next(c for c in collections if c["slug"] == "amazon-top-picks")
        self.assertEqual(len(top["items"]), 1)
        self.assertIn("Top by Clicks", top["items"][0]["badges"])
        self.assertIn("Top by Earnings", top["items"][0]["badges"])

    def test_curated_collection_created_from_workbook(self):
        records = [
            self.at.AmazonTrendRecord(asin="B010", collection_name="Mom Picks", source_list_type="collection", rank=1),
            self.at.AmazonTrendRecord(asin="B011", collection_name="Mom Picks", source_list_type="collection", rank=2),
        ]
        parsed = {"2A": [], "2B": [], "2C": [], "collections": records}
        collections = self.builder.from_workbook(parsed)
        slugs = [c["slug"] for c in collections]
        self.assertTrue(any("mom" in s for s in slugs))

    def test_item_sku_holds_asin(self):
        parsed = {
            "2A": [self._make_record("B099", "2A", rank=1)],
            "2B": [], "2C": [], "collections": [],
        }
        collections = self.builder.from_workbook(parsed)
        top = next(c for c in collections if c["slug"] == "amazon-top-picks")
        self.assertEqual(top["items"][0]["sku"], "B099")


# ---------------------------------------------------------------------------
# Chat non-breaking smoke test
# ---------------------------------------------------------------------------

class TestChatNonBreakingWithMixedCollections(unittest.TestCase):
    """Verify landing_page_data doesn't raise with mixed-retailer collections present."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        self.store = walmart_trends.WalmartTrendStore()
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def test_landing_page_data_succeeds_with_mixed_collections(self):
        conn = self.wt._connect()
        # Walmart product + collection
        conn.execute("INSERT OR IGNORE INTO walmart_products (sku, item_name) VALUES ('W1', 'Walmart Thing')")
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections (slug, name, source_type, is_active, display_order, metadata_json, retailer) "
            "VALUES ('w-mix', 'Walmart Mix', 'workbook_bootstrap', 1, 1, '{}', 'walmart')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collection_items (collection_slug, sku, display_order, badges_json, retailer) "
            "VALUES ('w-mix', 'W1', 0, '[]', 'walmart')"
        )
        # Amazon product + collection
        conn.execute("INSERT OR IGNORE INTO amazon_trend_products (asin, product_title) VALUES ('A1', 'Amazon Thing')")
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections (slug, name, source_type, is_active, display_order, metadata_json, retailer) "
            "VALUES ('a-mix', 'Amazon Mix', 'amazon_workbook_bootstrap', 1, 2, '{}', 'amazon')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collection_items (collection_slug, sku, display_order, badges_json, retailer) "
            "VALUES ('a-mix', 'A1', 0, '[]', 'amazon')"
        )
        conn.commit()
        conn.close()

        # Must not raise
        try:
            data = self.store.landing_page_data()
        except Exception as exc:
            self.fail(f"landing_page_data raised with mixed collections: {exc}")

        self.assertEqual(len(data["collections"]), 2)

    def test_storefront_chat_sessions_table_unaffected(self):
        """storefront_chat_sessions table should still exist and be queryable."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM storefront_chat_sessions LIMIT 1").fetchall()
            self.assertIsInstance(rows, list)
        except sqlite3.OperationalError as exc:
            self.fail(f"storefront_chat_sessions table missing or broken: {exc}")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Parser fix: _find_header_row_index + _read_workbook_rows override
# ---------------------------------------------------------------------------

class TestFindHeaderRowIndex(unittest.TestCase):
    """WorkbookTrendParser._find_header_row_index static method."""

    def setUp(self):
        import walmart_trends
        self.wt = walmart_trends

    def test_finds_header_in_title_blank_header_pattern(self):
        raw_rows = [
            ["Top Amazon Products — Clicks First", "", "", ""],  # row 0: title
            ["", "", "", ""],                                     # row 1: blank
            ["ASIN", "Product Title", "Amazon Link", "Rank"],    # row 2: real headers
            ["B001", "Widget", "https://amazon.com/dp/B001", "1"],
        ]
        idx = self.wt.WorkbookTrendParser._find_header_row_index(raw_rows, "ASIN")
        self.assertEqual(idx, 2)

    def test_returns_zero_when_key_not_found(self):
        raw_rows = [
            ["Col A", "Col B"],
            ["val1", "val2"],
        ]
        idx = self.wt.WorkbookTrendParser._find_header_row_index(raw_rows, "ASIN")
        self.assertEqual(idx, 0)

    def test_returns_zero_when_key_col_empty(self):
        raw_rows = [
            ["ASIN", "Title"],
            ["B001", "Widget"],
        ]
        idx = self.wt.WorkbookTrendParser._find_header_row_index(raw_rows, "")
        self.assertEqual(idx, 0)

    def test_case_insensitive_match(self):
        raw_rows = [
            ["title row"],
            ["asin", "Product Title"],
        ]
        idx = self.wt.WorkbookTrendParser._find_header_row_index(raw_rows, "ASIN")
        self.assertEqual(idx, 1)

    def test_returns_zero_on_empty_rows(self):
        idx = self.wt.WorkbookTrendParser._find_header_row_index([], "ASIN")
        self.assertEqual(idx, 0)

    def test_collection_sheet_key(self):
        raw_rows = [
            ["Curated Collections Export"],
            [""],
            ["Collection", "ASIN", "Rank"],
        ]
        idx = self.wt.WorkbookTrendParser._find_header_row_index(raw_rows, "Collection")
        self.assertEqual(idx, 2)


class TestAmazonParserHeaderDetection(unittest.TestCase):
    """AmazonWorkbookParser correctly skips title/blank rows when building dicts."""

    def setUp(self):
        import amazon_trends
        import walmart_trends
        self.at = amazon_trends
        self.wt = walmart_trends

    def _make_raw_rows_for_sheet(self, title, headers, data_rows):
        """Simulate Amazon workbook sheet: title → blank → headers → data."""
        return [
            [title] + [""] * (len(headers) - 1),
            [""] * len(headers),
            list(headers),
        ] + [list(row) for row in data_rows]

    def test_header_key_covers_all_non_summary_sheets(self):
        """Every required/optional sheet has an entry in _HEADER_KEY."""
        parser_cls = self.at.AmazonWorkbookParser
        all_sheets = list(parser_cls.REQUIRED_SHEETS) + list(parser_cls.OPTIONAL_SHEETS)
        for sheet in all_sheets:
            if sheet == "Summary":
                continue
            self.assertIn(
                sheet, parser_cls._HEADER_KEY,
                f"Sheet '{sheet}' missing from _HEADER_KEY",
            )

    def test_find_header_row_index_with_asin_sheet(self):
        raw = self._make_raw_rows_for_sheet(
            "Trending - Clicks First",
            ["ASIN", "Product Title", "Amazon Link", "Rank"],
            [["B001", "Widget", "https://amazon.com/dp/B001", "1"]],
        )
        idx = self.wt.WorkbookTrendParser._find_header_row_index(raw, "ASIN")
        self.assertEqual(idx, 2)

    def test_find_header_row_index_with_collection_sheet(self):
        raw = self._make_raw_rows_for_sheet(
            "Curated Collections",
            ["Collection", "ASIN", "Rank"],
            [["Mom Picks", "B002", "1"]],
        )
        idx = self.wt.WorkbookTrendParser._find_header_row_index(raw, "Collection")
        self.assertEqual(idx, 2)

    def test_dict_keys_use_real_column_names(self):
        """Simulate what _read_workbook_rows would produce from title+blank+header+data."""
        raw = self._make_raw_rows_for_sheet(
            "Trending - Clicks First",
            ["ASIN", "Product Title", "Amazon Link", "Rank"],
            [["B001", "Widget", "https://amazon.com/dp/B001", "1"]],
        )
        key_col = "ASIN"
        header_idx = self.wt.WorkbookTrendParser._find_header_row_index(raw, key_col)
        headers = [h.strip() for h in raw[header_idx]]
        rows = []
        for data_row in raw[header_idx + 1:]:
            if not any(data_row):
                continue
            rows.append({headers[i]: data_row[i] if i < len(data_row) else "" for i in range(len(headers))})

        self.assertEqual(len(rows), 1)
        self.assertIn("ASIN", rows[0])
        self.assertIn("Product Title", rows[0])
        self.assertEqual(rows[0]["ASIN"], "B001")
        self.assertNotIn("col0", rows[0], "dict keys should be real column names, not col0 positionals")


@unittest.skipUnless(
    os.path.exists(
        os.path.join(os.path.dirname(__file__), "..", "attached_assets", "Amazon_May12_Analysis.xlsx")
    ),
    "Amazon_May12_Analysis.xlsx not present — skipping live workbook integration test",
)
class TestAmazonWorkbookIntegration(unittest.TestCase):
    """Live parse of Amazon_May12_Analysis.xlsx — skipped when file absent."""

    @classmethod
    def setUpClass(cls):
        import amazon_trends
        cls.at = amazon_trends
        workbook_path = os.path.join(
            os.path.dirname(__file__), "..", "attached_assets", "Amazon_May12_Analysis.xlsx"
        )
        parser = cls.at.AmazonWorkbookParser(workbook_path)
        cls.result = parser.parse()

    def test_parse_returns_nonempty_clicks_first(self):
        self.assertTrue(len(self.result["2A"]) > 0, "2A (Clicks First) should have records")

    def test_records_have_real_asin(self):
        for record in self.result["2A"][:5]:
            self.assertTrue(record.asin.startswith("B0") or len(record.asin) == 10,
                            f"Unexpected ASIN format: {record.asin!r}")

    def test_records_have_product_title(self):
        for record in self.result["2A"][:5]:
            self.assertNotEqual(record.product_title, "", "product_title should not be empty")
            self.assertNotIn("Top Amazon", record.product_title,
                             "product_title should not contain the sheet title row")

    def test_amazon_links_present(self):
        links_present = sum(1 for r in self.result["2A"] if r.amazon_link)
        self.assertGreater(links_present, 0, "Some records should have Amazon links")

    def test_collections_parsed(self):
        self.assertTrue(len(self.result["collections"]) > 0, "collections list should not be empty")

    def test_no_title_row_leaked_as_asin(self):
        all_asins = [r.asin for r in self.result["2A"] + self.result["2B"] + self.result["2C"]]
        for asin in all_asins:
            self.assertNotIn("Top Amazon", asin, f"Title row leaked as ASIN: {asin!r}")
            self.assertNotIn("Trending", asin, f"Sheet name leaked as ASIN: {asin!r}")


if __name__ == "__main__":
    unittest.main()
