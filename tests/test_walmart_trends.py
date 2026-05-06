import importlib
import os
import tempfile
import unittest
from pathlib import Path


class WalmartTrendsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "walmart_trends_test.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        import db_schema
        import walmart_trends

        db_schema.DB_PATH = self.db_path
        walmart_trends.DB_PATH = self.db_path
        import sqlite3
        conn = sqlite3.connect(self.db_path)
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
        self.db_schema = db_schema
        self.wt = walmart_trends

    def tearDown(self):
        self.tmp.cleanup()

    def test_workbook_parser_happy_path(self):
        parsed = self.wt.WorkbookTrendParser("attached_assets/Walmart_May6th_Analysis.xlsx").parse()
        self.assertEqual(len(parsed["1A"]), 10)
        self.assertEqual(len(parsed["1B"]), 10)
        self.assertGreaterEqual(len(parsed["collections"]), 80)

    def test_workbook_parser_missing_sheet_failure(self):
        parser = self.wt.WorkbookTrendParser("attached_assets/Walmart_May6th_Analysis.xlsx")
        with self.assertRaises(self.wt.WorkbookValidationError):
            parser._validate({"Trending - Item Count First": [{"SKU": "1"}]})

    def test_top_sellers_dedupe_and_badges(self):
        builder = self.wt.CollectionBuilder()
        by_units = [self.wt.TrendRecord(sku="1", item_count=5, total_earnings=1, source_list_type="1A", rank=1)]
        by_earnings = [
            self.wt.TrendRecord(sku="1", item_count=5, total_earnings=1, source_list_type="1B", rank=1),
            self.wt.TrendRecord(sku="2", item_count=1, total_earnings=10, source_list_type="1B", rank=2),
        ]
        top = builder._top_sellers(by_units, by_earnings)
        self.assertEqual(len(top["items"]), 2)
        item_one = next(item for item in top["items"] if item["sku"] == "1")
        self.assertEqual(set(item_one["badges"]), {"Top by Units", "Top by Earnings"})

    def test_fallback_affiliate_link_reuse(self):
        store = self.wt.WalmartTrendStore()
        store.save_affiliate_link("sku1", "https://www.walmart.com/ip/sku1", "https://www.walmart.com/ip/sku1", status="fallback")
        service = self.wt.AffiliateLinkService(store)
        self.assertEqual(
            service.ensure("sku1", "https://www.walmart.com/ip/sku1"),
            "https://www.walmart.com/ip/sku1",
        )

    def test_fallback_urlgenius_link_reuse(self):
        store = self.wt.WalmartTrendStore()
        store.save_urlgenius_link("https://impact.example/sku1", "https://impact.example/sku1", status="fallback")
        service = self.wt.URLGeniusLinkService(store)
        self.assertEqual(service.ensure("https://impact.example/sku1", "sku1"), "https://impact.example/sku1")

    def test_refresh_lock_prevents_overlap(self):
        store = self.wt.WalmartTrendStore()
        store.create_run("workbook_bootstrap")
        with self.assertRaises(self.wt.RefreshAlreadyRunning):
            store.create_run("impact_weekly")

    def test_failed_weekly_run_keeps_active_collections(self):
        store = self.wt.WalmartTrendStore()
        run_id = store.create_run("workbook_bootstrap")
        record = self.wt.TrendRecord(sku="sku1", item_name="Existing Product", category_list="Home")
        store.upsert_product_from_record(record)
        store.replace_collections(run_id, "workbook_bootstrap", [{
            "slug": "top-sellers",
            "name": "Top Sellers",
            "description": "Existing",
            "items": [{"sku": "sku1", "badges": ["Top by Units"]}],
        }])
        store.finish_run(run_id, "success", {"records": 1}, [])

        original = self.wt.ImpactPerformanceService.fetch_latest_7_days
        self.wt.ImpactPerformanceService.fetch_latest_7_days = lambda _self: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = self.wt.WalmartTrendRefreshService().refresh_from_impact()
        finally:
            self.wt.ImpactPerformanceService.fetch_latest_7_days = original

        self.assertEqual(result.status, "failed")
        page = store.landing_page_data()
        self.assertEqual(page["collections"][0]["slug"], "top-sellers")
        self.assertEqual(page["collections"][0]["items"][0]["sku"], "sku1")


if __name__ == "__main__":
    unittest.main()
