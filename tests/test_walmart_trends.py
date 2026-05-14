import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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

    def test_raw_walmart_affiliate_link_is_replaced_with_manual_goto(self):
        store = self.wt.WalmartTrendStore()
        product_url = "https://www.walmart.com/ip/sku1"
        store.save_affiliate_link("sku1", product_url, product_url, status="fallback")
        service = self.wt.AffiliateLinkService(store)

        link = service.ensure("sku1", product_url)

        self.assertTrue(link.startswith("https://goto.walmart.com/c/3590891/1398372/16662?"))
        self.assertIn("u=https%3A%2F%2Fwww.walmart.com%2Fip%2Fsku1", link)
        self.assertNotEqual(link, product_url)


    def test_missing_impact_token_uses_primary_manual_goto(self):
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
            "https://goto.walmart.com/c/6365428/1398372/16662?veh=aff"
            "&u=https%253A%252F%252Fwww.walmart.com%252Fip%252F5454929532"
        )
        fixed = (
            "https://goto.walmart.com/c/3590891/1398372/16662?veh=aff"
            "&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        )

        self.assertTrue(self.wt.is_malformed_double_encoded_walmart_goto(broken))
        self.assertFalse(self.wt.is_malformed_double_encoded_walmart_goto(fixed))

    def test_normalize_product_brand_rejects_source_domains_and_preserves_real_brands(self):
        self.assertEqual(self.wt.normalize_product_brand("WalmartCreator.com"), "")
        self.assertEqual(self.wt.normalize_product_brand("https://walmart.com/ip/123"), "")
        self.assertEqual(self.wt.normalize_product_brand("Best Choice Products"), "Best Choice Products")
        self.assertEqual(self.wt.normalize_product_brand("Better Homes & Gardens"), "Better Homes & Gardens")

    def test_normalize_product_brand_infers_known_safe_title_prefixes(self):
        self.assertEqual(self.wt.normalize_product_brand("", "CONCETTA 4-Piece Patio Furniture Set"), "CONCETTA")
        self.assertEqual(self.wt.normalize_product_brand("", "Better Homes & Gardens Lilah Patio Chair"), "Better Homes & Gardens")
        self.assertEqual(self.wt.normalize_product_brand("", "Sportspower BouncePro Trampoline"), "Sportspower")
        self.assertEqual(self.wt.normalize_product_brand("", "JUMPZYLLA Trampoline with Enclosure"), "JUMPZYLLA")
        self.assertEqual(self.wt.normalize_product_brand("", "Generic Patio Furniture Set"), "")

    def test_update_product_enrichment_does_not_preserve_invalid_existing_brand(self):
        store = self.wt.WalmartTrendStore()
        store.upsert_product_from_record(self.wt.TrendRecord(
            sku="18985723227",
            item_name="Generic Patio Furniture Set",
            brand="WalmartCreator.com",
        ))

        store.update_product_enrichment(
            "18985723227",
            {"title": "Patio Furniture Set", "brand": ""},
            "ok",
        )

        product = store.get_product("18985723227")
        self.assertEqual(product["brand"], "")

    def test_update_product_enrichment_can_infer_concetta_from_title(self):
        store = self.wt.WalmartTrendStore()
        store.upsert_product_from_record(self.wt.TrendRecord(
            sku="18985723227",
            item_name="Generic Patio Furniture Set",
            brand="WalmartCreator.com",
        ))

        store.update_product_enrichment(
            "18985723227",
            {"title": "CONCETTA 4-Piece Patio Furniture Set with Loveseat", "brand": ""},
            "ok",
        )

        product = store.get_product("18985723227")
        self.assertEqual(product["brand"], "CONCETTA")


    def test_vanity_goto_affiliate_link_is_not_reused(self):
        store = self.wt.WalmartTrendStore()
        product_url = "https://www.walmart.com/ip/5454929532"
        stale = "https://goto.walmart.com/WONqy3?utm_source=walmart&utm_medium=affiliate"
        store.save_affiliate_link("5454929532", product_url, stale, status="active")
        service = self.wt.AffiliateLinkService(store)

        link = service.ensure("5454929532", product_url)

        self.assertNotEqual(link, stale)
        self.assertTrue(link.startswith("https://goto.walmart.com/c/3590891/1398372/16662?"))
        self.assertIn("sourceid=imp_000011112222333344", link)
        self.assertIn("u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532", link)

    def test_old_creator_goto_path_is_stale(self):
        old_creator = (
            "https://goto.walmart.com/c/6365428/1398372/16662?"
            "subId1=walmart-trending&subId2=5454929532&subId3=&sourceid=imp_000011112222333344"
            "&veh=aff&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        )
        current_creator = (
            "https://goto.walmart.com/c/3590891/1398372/16662?"
            "subId1=walmart-trending&subId2=5454929532&subId3=&sourceid=imp_000011112222333344"
            "&veh=aff&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        )

        self.assertEqual(self.wt.stale_walmart_link_reason(old_creator), "stored Walmart affiliate URL uses old creator goto path")
        self.assertEqual(self.wt.stale_walmart_link_reason(current_creator), "")


    def test_walmart_goto_with_dirty_embedded_destination_is_stale(self):
        dirty = (
            "https://goto.walmart.com/c/3590891/1398372/16662?subId1=walmart-trending"
            "&subId2=5454929532&subId3=&sourceid=imp_000011112222333344&veh=aff"
            "&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532%3Firgwc%3D1%26clickid%3Dabc%26utm_source%3Decho"
        )

        self.assertEqual(
            self.wt.stale_walmart_link_reason(dirty),
            "embedded Walmart destination contains prior affiliate params",
        )

    def test_walmart_goto_missing_sourceid_is_stale(self):
        missing_sourceid = (
            "https://goto.walmart.com/c/3590891/1398372/16662?subId1=walmart-trending"
            "&subId2=5454929532&subId3=&veh=aff"
            "&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        )

        self.assertEqual(
            self.wt.stale_walmart_link_reason(missing_sourceid),
            "stored Walmart affiliate URL missing required sourceid",
        )

    def test_walmart_goto_with_nested_goto_destination_is_stale(self):
        nested = (
            "https://goto.walmart.com/c/3590891/1398372/16662?subId1=walmart-trending"
            "&subId2=5454929532&subId3=&sourceid=imp_000011112222333344&veh=aff"
            "&u=https%3A%2F%2Fgoto.walmart.com%2FWONqy3%3Fu%3Dhttps%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        )

        self.assertEqual(
            self.wt.stale_walmart_link_reason(nested),
            "embedded Walmart destination is itself an affiliate goto link",
        )

    def test_stale_double_encoded_affiliate_link_is_not_reused(self):
        store = self.wt.WalmartTrendStore()
        product_url = "https://www.walmart.com/ip/5454929532"
        stale = (
            "https://goto.walmart.com/c/6365428/1398372/16662?veh=aff"
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
            "https://goto.walmart.com/c/6365428/1398372/16662?veh=aff"
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
        destination = "https://goto.walmart.com/c/3590891/1398372/16662?subId1=walmart-trending&subId2=5454929532&subId3=&sourceid=imp_000011112222333344&veh=aff&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532"
        store.save_urlgenius_link(destination, "https://urlgeni.us/walmart/dQB0MO")

        original_key = os.environ.get("URLGENIUS_API_KEY")
        os.environ["URLGENIUS_API_KEY"] = "test-key"
        try:
            service = self.wt.URLGeniusLinkService(store)
            with patch.object(
                service,
                "_first_hop_redirect",
                return_value=(
                    "https://goto.walmart.com/c/6365428/1398372/16662?veh=aff"
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


    def test_regeneration_service_inspects_and_regenerates_stale_sku(self):
        store = self.wt.WalmartTrendStore()
        sku = "5454929532"
        product_url = f"https://www.walmart.com/ip/{sku}"
        stale_impact = (
            "https://goto.walmart.com/c/6365428/1398372/16662?veh=aff"
            f"&u=https%253A%252F%252Fwww.walmart.com%252Fip%252F{sku}"
        )
        fresh_impact = (
            "https://goto.walmart.com/c/3590891/1398372/16662?subId1=walmart-trending"
            f"&subId2={sku}&subId3=&sourceid=imp_000011112222333344&veh=aff"
            f"&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F{sku}"
        )
        store.save_affiliate_link(sku, product_url, stale_impact, status="active")
        store.save_urlgenius_link(stale_impact, "https://urlgeni.us/walmart/dQB0MO", status="active")

        original_impact_token = os.environ.get("IMPACT_AUTH_TOKEN")
        original_urlgenius_key = os.environ.get("URLGENIUS_API_KEY")
        os.environ["IMPACT_AUTH_TOKEN"] = "impact-token"
        os.environ["URLGENIUS_API_KEY"] = "urlgenius-key"
        try:
            service = self.wt.WalmartLinkRegenerationService(store)
            service.affiliates.client.generate_walmart_link = lambda *args, **kwargs: fresh_impact
            service.urlgenius.client.create_link = lambda *args, **kwargs: {
                "link": {"genius_url": "https://urlgeni.us/walmart/fresh", "id": "fresh-id"}
            }

            before = service.inspect_sku(sku)
            result = service.regenerate_sku(sku)
            after = service.inspect_sku(sku)
        finally:
            if original_impact_token is None:
                os.environ.pop("IMPACT_AUTH_TOKEN", None)
            else:
                os.environ["IMPACT_AUTH_TOKEN"] = original_impact_token
            if original_urlgenius_key is None:
                os.environ.pop("URLGENIUS_API_KEY", None)
            else:
                os.environ["URLGENIUS_API_KEY"] = original_urlgenius_key

        self.assertTrue(before["affiliate_stale"])
        self.assertTrue(before["urlgenius_stale"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["fresh_impact_url"], fresh_impact)
        self.assertEqual(result["fresh_genius_url"], "https://urlgeni.us/walmart/fresh")
        self.assertFalse(after["affiliate_stale"])
        self.assertFalse(after["urlgenius_stale"])
        self.assertEqual(after["impact_url"], fresh_impact)
        self.assertEqual(after["genius_url"], "https://urlgeni.us/walmart/fresh")
        self.assertIsNone(store.current_urlgenius_for_destination(stale_impact))

    def test_regenerate_all_stale_finds_urlgenius_destination_by_sku_pattern(self):
        store = self.wt.WalmartTrendStore()
        sku = "5454929532"
        stale_destination = (
            "https://goto.walmart.com/c/6365428/1398372/16662?veh=aff"
            f"&u=https%253A%252F%252Fwww.walmart.com%252Fip%252F{sku}"
        )
        fresh_impact = (
            "https://goto.walmart.com/c/3590891/1398372/16662?subId1=walmart-trending"
            f"&subId2={sku}&subId3=&sourceid=imp_000011112222333344&veh=aff"
            f"&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F{sku}"
        )
        store.save_urlgenius_link(stale_destination, "https://urlgeni.us/walmart/dQB0MO", status="active")

        original_impact_token = os.environ.get("IMPACT_AUTH_TOKEN")
        original_urlgenius_key = os.environ.get("URLGENIUS_API_KEY")
        os.environ["IMPACT_AUTH_TOKEN"] = "impact-token"
        os.environ["URLGENIUS_API_KEY"] = "urlgenius-key"
        try:
            service = self.wt.WalmartLinkRegenerationService(store)
            service.affiliates.client.generate_walmart_link = lambda *args, **kwargs: fresh_impact
            service.urlgenius.client.create_link = lambda *args, **kwargs: {
                "link": {"genius_url": "https://urlgeni.us/walmart/fresh", "id": "fresh-id"}
            }

            result = service.regenerate_all_stale()
        finally:
            if original_impact_token is None:
                os.environ.pop("IMPACT_AUTH_TOKEN", None)
            else:
                os.environ["IMPACT_AUTH_TOKEN"] = original_impact_token
            if original_urlgenius_key is None:
                os.environ.pop("URLGENIUS_API_KEY", None)
            else:
                os.environ["URLGENIUS_API_KEY"] = original_urlgenius_key

        self.assertEqual(result["stale_skus_found"], 1)
        self.assertEqual(result["regenerated_count"], 1)
        after = service.inspect_sku(sku)
        self.assertEqual(after["impact_url"], fresh_impact)
        self.assertEqual(after["genius_url"], "https://urlgeni.us/walmart/fresh")

    def test_rebuild_all_forces_fresh_affiliate_and_urlgenius_rows(self):
        import sqlite3
        store = self.wt.WalmartTrendStore()
        sku = "5454929532"
        product_url = f"https://www.walmart.com/ip/{sku}?irgwc=1&clickid=old&utm_source=echo"
        store.upsert_product_from_record(self.wt.TrendRecord(sku=sku, item_name="Test Product"))
        store.update_product_enrichment(sku, {"canonical_url": product_url}, "ok")
        old_impact = (
            "https://goto.walmart.com/c/3590891/1398372/16662?subId1=walmart-trending"
            f"&subId2={sku}&subId3=&sourceid=imp_000011112222333344&veh=aff"
            f"&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F{sku}"
        )
        store.save_affiliate_link(sku, product_url, old_impact, status="active")
        store.save_urlgenius_link(old_impact, "https://urlgeni.us/walmart/old", status="active")

        original_impact_sid = os.environ.get("IMPACT_ACCOUNT_SID")
        original_impact_token = os.environ.get("IMPACT_AUTH_TOKEN")
        original_urlgenius_key = os.environ.get("URLGENIUS_API_KEY")
        os.environ["IMPACT_ACCOUNT_SID"] = "acct-sid"
        os.environ["IMPACT_AUTH_TOKEN"] = "impact-token"
        os.environ["URLGENIUS_API_KEY"] = "urlgenius-key"
        impact_response = Mock()
        impact_response.raise_for_status.return_value = None
        impact_response.json.return_value = {"TrackingURL": "https://impact.example/tracking/5454929532"}
        try:
            with patch("product_api.requests.post", return_value=impact_response) as impact_post:
                service = self.wt.WalmartLinkRegenerationService(store)
                calls = []

                def create_link(destination_url, **kwargs):
                    calls.append((destination_url, kwargs))
                    return {"link": {"genius_url": "https://urlgeni.us/walmart/fresh", "id": "fresh-id"}}

                service.urlgenius.client.create_link = create_link
                dry_run = service.rebuild_all(limit=1, dry_run=True)
                result = service.rebuild_all(limit=1)
                impact_call = impact_post.call_args
        finally:
            if original_impact_sid is None:
                os.environ.pop("IMPACT_ACCOUNT_SID", None)
            else:
                os.environ["IMPACT_ACCOUNT_SID"] = original_impact_sid
            if original_impact_token is None:
                os.environ.pop("IMPACT_AUTH_TOKEN", None)
            else:
                os.environ["IMPACT_AUTH_TOKEN"] = original_impact_token
            if original_urlgenius_key is None:
                os.environ.pop("URLGENIUS_API_KEY", None)
            else:
                os.environ["URLGENIUS_API_KEY"] = original_urlgenius_key

        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["selected_skus"], 1)
        self.assertEqual(result["rebuilt_count"], 1)
        self.assertEqual(impact_call.args[0], "https://api.impact.com/Mediapartners/acct-sid/Programs/16662/TrackingLinks")
        self.assertEqual(impact_call.kwargs["auth"], ("acct-sid", "impact-token"))
        self.assertEqual(impact_call.kwargs["data"], {
            "DeepLink": f"https://www.walmart.com/ip/{sku}?utm_source=echo",
            "subId1": "walmart-trending",
            "subId2": sku,
            "subId3": "",
        })
        self.assertNotIn("Type", impact_call.kwargs["data"])
        self.assertTrue(calls)
        fresh_impact = result["results"][0]["fresh_impact_url"]
        self.assertEqual(fresh_impact, "https://impact.example/tracking/5454929532")
        self.assertEqual(calls[0][0], fresh_impact)
        self.assertTrue(calls[0][1]["force_new"])

        after = service.inspect_sku(sku)
        self.assertEqual(after["impact_url"], fresh_impact)
        self.assertEqual(after["genius_url"], "https://urlgeni.us/walmart/fresh")

        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            stale_affiliates = conn.execute(
                "SELECT * FROM walmart_affiliate_links WHERE sku = ? AND status = 'stale'", (sku,)
            ).fetchall()
            stale_urlgenius = conn.execute(
                "SELECT * FROM walmart_urlgenius_links WHERE status = 'stale'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(stale_affiliates), 1)
        self.assertIn("#stale-affiliate-", stale_affiliates[0]["product_url"])
        self.assertEqual(len(stale_urlgenius), 1)
        self.assertIn("#stale-urlgenius-", stale_urlgenius[0]["destination_url"])

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
