import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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


    def test_missing_impact_token_uses_manual_goto_fallback(self):
        original_token = os.environ.pop("IMPACT_AUTH_TOKEN", None)
        try:
            store = self.wt.WalmartTrendStore()
            service = self.wt.AffiliateLinkService(store)
            link = service.ensure("sku1", "https%3A%2F%2Fwww.walmart.com%2Fip%2Fsku1")
        finally:
            if original_token is not None:
                os.environ["IMPACT_AUTH_TOKEN"] = original_token

        self.assertTrue(link.startswith("https://goto.walmart.com/c/3590891/1398372/16662?"))
        self.assertIn("u=https%3A%2F%2Fwww.walmart.com%2Fip%2Fsku1", link)
        self.assertNotIn("https%253A%252F%252Fwww.walmart.com%252Fip%252Fsku1", link)

    def test_fallback_urlgenius_link_reuse(self):
        store = self.wt.WalmartTrendStore()
        store.save_urlgenius_link("https://impact.example/sku1", "https://impact.example/sku1", status="fallback")
        service = self.wt.URLGeniusLinkService(store)
        self.assertEqual(service.ensure("https://impact.example/sku1", "sku1"), "https://impact.example/sku1")

    def test_double_encoded_walmart_goto_detection(self):
        broken = (
            "https://goto.walmart.com/c/3590891/1398372/16662?veh=aff"
            "&u=https%253A%252F%252Fwww.walmart.com%252Fip%252F5454929532"
        )
        fixed = (
            "https://goto.walmart.com/c/3590891/1398372/16662?veh=aff"
            "&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        )

        self.assertTrue(self.wt.is_malformed_double_encoded_walmart_goto(broken))
        self.assertFalse(self.wt.is_malformed_double_encoded_walmart_goto(fixed))

    def test_stale_double_encoded_affiliate_link_is_not_reused(self):
        store = self.wt.WalmartTrendStore()
        product_url = "https://www.walmart.com/ip/5454929532"
        stale = (
            "https://goto.walmart.com/c/3590891/1398372/16662?veh=aff"
            "&u=https%253A%252F%252Fwww.walmart.com%252Fip%252F5454929532"
        )
        store.save_affiliate_link("5454929532", product_url, stale, status="fallback")

        original_token = os.environ.pop("IMPACT_AUTH_TOKEN", None)
        try:
            service = self.wt.AffiliateLinkService(store)
            link = service.ensure("5454929532", product_url)
        finally:
            if original_token is not None:
                os.environ["IMPACT_AUTH_TOKEN"] = original_token

        self.assertNotEqual(link, stale)
        self.assertIn("u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532", link)
        self.assertNotIn("u=https%253A%252F%252Fwww.walmart.com%252Fip%252F5454929532", link)

    def test_stale_double_encoded_urlgenius_destination_forces_fresh_link(self):
        store = self.wt.WalmartTrendStore()
        stale_destination = (
            "https://goto.walmart.com/c/3590891/1398372/16662?veh=aff"
            "&u=https%253A%252F%252Fwww.walmart.com%252Fip%252F5454929532"
        )
        store.save_urlgenius_link(stale_destination, "https://urlgeni.us/walmart/dQB0MO")

        original_key = os.environ.get("URLGENIUS_API_KEY")
        os.environ["URLGENIUS_API_KEY"] = "test-key"
        try:
            service = self.wt.URLGeniusLinkService(store)
            with patch.object(service.client, "create_link", return_value={"link": {"genius_url": "https://urlgeni.us/walmart/fresh", "id": "fresh-id"}}) as create:
                link = service.ensure(stale_destination, "5454929532")
        finally:
            if original_key is None:
                os.environ.pop("URLGENIUS_API_KEY", None)
            else:
                os.environ["URLGENIUS_API_KEY"] = original_key

        self.assertEqual(link, "https://urlgeni.us/walmart/fresh")
        self.assertTrue(create.call_args.kwargs["force_new"])

    def test_stale_urlgenius_first_hop_redirect_forces_fresh_link(self):
        store = self.wt.WalmartTrendStore()
        destination = "https://goto.walmart.com/c/3590891/1398372/16662?veh=aff&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        store.save_urlgenius_link(destination, "https://urlgeni.us/walmart/dQB0MO")

        original_key = os.environ.get("URLGENIUS_API_KEY")
        os.environ["URLGENIUS_API_KEY"] = "test-key"
        try:
            service = self.wt.URLGeniusLinkService(store)
            with patch.object(
                service,
                "_first_hop_redirect",
                return_value=(
                    "https://goto.walmart.com/c/3590891/1398372/16662?veh=aff"
                    "&u=https%253A%252F%252Fwww.walmart.com%252Fip%252F5454929532"
                ),
            ), patch.object(
                service.client,
                "create_link",
                return_value={"link": {"genius_url": "https://urlgeni.us/walmart/fresh", "id": "fresh-id"}},
            ) as create:
                link = service.ensure(destination, "5454929532")
        finally:
            if original_key is None:
                os.environ.pop("URLGENIUS_API_KEY", None)
            else:
                os.environ["URLGENIUS_API_KEY"] = original_key

        self.assertEqual(link, "https://urlgeni.us/walmart/fresh")
        self.assertTrue(create.call_args.kwargs["force_new"])

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
