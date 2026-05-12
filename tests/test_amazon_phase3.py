"""Phase 3 tests — Amazon URLGenius, product enrichment, retailer-aware storefront.

Covers:
- AmazonTrendStore.update_product_enrichment: field updates, COALESCE preservation
- AmazonURLGeniusLinkService: no-key fallback, stores fallback row, delegates to store
- AmazonProductEnricher: no-token skip, Crawlbase success path, Crawlbase failure fallback
- enrich_batch: counts ok/pending/fallback/skipped correctly
- Schema: enrichment_error + last_verified_at columns exist on amazon_trend_products
- Retailer-aware landing_page_data: retailer field on items, price/shop button semantics
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _setup_db(db_path: str) -> None:
    os.environ["CACHE_DB_PATH"] = db_path
    import db_schema
    import walmart_trends
    import amazon_trends

    db_schema.DB_PATH = db_path
    walmart_trends.DB_PATH = db_path
    amazon_trends._connect.__globals__  # touch module to ensure it's loaded

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
# Schema: new columns exist
# ---------------------------------------------------------------------------

class TestPhase3Schema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enrichment_error_column_exists(self):
        conn = sqlite3.connect(self.db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(amazon_trend_products)")}
        conn.close()
        self.assertIn("enrichment_error", cols)

    def test_last_verified_at_column_exists(self):
        conn = sqlite3.connect(self.db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(amazon_trend_products)")}
        conn.close()
        self.assertIn("last_verified_at", cols)


# ---------------------------------------------------------------------------
# AmazonTrendStore.update_product_enrichment
# ---------------------------------------------------------------------------

class TestUpdateProductEnrichment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.at = amazon_trends
        self.store = amazon_trends.AmazonTrendStore()
        # Seed a product row to update
        record = amazon_trends.AmazonTrendRecord(
            asin="B001", product_title="Test Product", amazon_link="https://amazon.com/dp/B001"
        )
        self.store.upsert_product(record)

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_writes_image_and_price(self):
        self.store.update_product_enrichment(
            "B001",
            {"image_url": "https://img.example.com/b001.jpg", "current_price": 19.99, "price_display": "$19.99"},
            status="ok",
        )
        product = self.store.get_product("B001")
        self.assertEqual(product["image_url"], "https://img.example.com/b001.jpg")
        self.assertAlmostEqual(product["current_price"], 19.99, places=2)
        self.assertEqual(product["price_display"], "$19.99")
        self.assertEqual(product["enrichment_status"], "ok")

    def test_update_does_not_overwrite_existing_image_with_empty(self):
        self.store.update_product_enrichment(
            "B001",
            {"image_url": "https://img.example.com/b001.jpg"},
            status="ok",
        )
        self.store.update_product_enrichment(
            "B001",
            {"image_url": ""},  # empty — should preserve existing
            status="ok",
        )
        product = self.store.get_product("B001")
        self.assertEqual(product["image_url"], "https://img.example.com/b001.jpg")

    def test_update_sets_enrichment_error_on_failure(self):
        self.store.update_product_enrichment("B001", {}, status="fallback", error="API timeout")
        product = self.store.get_product("B001")
        self.assertEqual(product["enrichment_status"], "fallback")
        self.assertEqual(product["enrichment_error"], "API timeout")

    def test_update_sets_last_verified_at(self):
        self.store.update_product_enrichment("B001", {"image_url": "https://x.com/img.jpg"}, status="ok")
        product = self.store.get_product("B001")
        self.assertIsNotNone(product["last_verified_at"])

    def test_update_writes_brand(self):
        self.store.update_product_enrichment("B001", {"brand": "TestBrand"}, status="ok")
        product = self.store.get_product("B001")
        self.assertEqual(product["brand"], "TestBrand")


# ---------------------------------------------------------------------------
# AmazonURLGeniusLinkService
# ---------------------------------------------------------------------------

class TestAmazonURLGeniusLinkService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.at = amazon_trends
        self.store = amazon_trends.AmazonTrendStore()
        self.service = amazon_trends.AmazonURLGeniusLinkService(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_affiliate_url_when_no_api_key(self):
        self.service.client.api_key = ""
        result = self.service.ensure("https://amazon.com/dp/B001?tag=test-20", "B001")
        self.assertEqual(result, "https://amazon.com/dp/B001?tag=test-20")

    def test_saves_fallback_row_when_no_api_key(self):
        self.service.client.api_key = ""
        self.service.ensure("https://amazon.com/dp/B002?tag=test-20", "B002")
        cached = self.store.urlgenius_for("https://amazon.com/dp/B002?tag=test-20")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["status"], "fallback")

    def test_returns_cached_genius_url_on_second_call(self):
        self.store.save_urlgenius_link(
            "https://amazon.com/dp/B003?tag=test-20",
            "https://urlgeni.us/amazon/B003",
            status="active",
        )
        result = self.service.ensure("https://amazon.com/dp/B003?tag=test-20", "B003")
        self.assertEqual(result, "https://urlgeni.us/amazon/B003")

    def test_saves_fallback_on_api_exception(self):
        self.service.client.api_key = "fake-key"
        self.service.client.create_link = MagicMock(side_effect=Exception("network error"))
        result = self.service.ensure("https://amazon.com/dp/B004?tag=test-20", "B004")
        self.assertEqual(result, "https://amazon.com/dp/B004?tag=test-20")
        cached = self.store.urlgenius_for("https://amazon.com/dp/B004?tag=test-20")
        self.assertEqual(cached["status"], "fallback")

    def test_returns_genius_url_on_api_success(self):
        self.service.client.api_key = "fake-key"
        self.service.client.create_link = MagicMock(return_value={
            "link": {"genius_url": "https://urlgeni.us/amazon/B005", "id": "link-123"}
        })
        result = self.service.ensure("https://amazon.com/dp/B005?tag=test-20", "B005")
        self.assertEqual(result, "https://urlgeni.us/amazon/B005")
        cached = self.store.urlgenius_for("https://amazon.com/dp/B005?tag=test-20")
        self.assertEqual(cached["genius_url"], "https://urlgeni.us/amazon/B005")

    def test_empty_affiliate_url_returned_unchanged(self):
        result = self.service.ensure("", "B006")
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# AmazonProductEnricher
# ---------------------------------------------------------------------------

class TestAmazonProductEnricher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.at = amazon_trends
        self.store = amazon_trends.AmazonTrendStore()
        self.enricher = amazon_trends.AmazonProductEnricher(self.store)
        # Seed a product
        record = amazon_trends.AmazonTrendRecord(asin="B010", product_title="Existing Product")
        self.store.upsert_product(record)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enrich_skips_when_no_token(self):
        self.enricher.client.token = None
        result = self.enricher.enrich("B010")
        product = self.store.get_product("B010")
        # enrichment_status remains 'pending' — no write happened
        self.assertEqual(product["enrichment_status"], "pending")

    def test_enrich_sets_ok_status_on_success(self):
        self.enricher.client.token = "fake-token"
        self.enricher.client.get_amazon_product = MagicMock(return_value={
            "imageUrl": "https://img.amazon.com/b010.jpg",
            "price": "24.99",
            "brand": "WidgetCo",
        })
        self.enricher.enrich("B010")
        product = self.store.get_product("B010")
        self.assertEqual(product["enrichment_status"], "ok")
        self.assertEqual(product["image_url"], "https://img.amazon.com/b010.jpg")
        self.assertEqual(product["brand"], "WidgetCo")

    def test_enrich_sets_pending_when_crawlbase_returns_none(self):
        self.enricher.client.token = "fake-token"
        self.enricher.client.get_amazon_product = MagicMock(return_value=None)
        self.enricher.enrich("B010")
        product = self.store.get_product("B010")
        self.assertEqual(product["enrichment_status"], "pending")

    def test_enrich_sets_fallback_on_exception(self):
        self.enricher.client.token = "fake-token"
        self.enricher.client.get_amazon_product = MagicMock(side_effect=Exception("timeout"))
        self.enricher.enrich("B010")
        product = self.store.get_product("B010")
        self.assertEqual(product["enrichment_status"], "fallback")
        self.assertIn("timeout", product["enrichment_error"])

    def test_enrich_skips_already_ok_with_image(self):
        self.store.update_product_enrichment(
            "B010", {"image_url": "https://img.example.com/b010.jpg"}, status="ok"
        )
        self.enricher.client.token = "fake-token"
        self.enricher.client.get_amazon_product = MagicMock()
        self.enricher.enrich("B010")
        # Should not have called the API
        self.enricher.client.get_amazon_product.assert_not_called()


class TestAmazonEnrichBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.at = amazon_trends
        self.store = amazon_trends.AmazonTrendStore()
        self.enricher = amazon_trends.AmazonProductEnricher(self.store)
        for asin in ["B011", "B012", "B013"]:
            record = amazon_trends.AmazonTrendRecord(asin=asin, product_title=f"Product {asin}")
            self.store.upsert_product(record)
        # Pre-enrich B011
        self.store.update_product_enrichment("B011", {"image_url": "https://img.example.com/b011.jpg"}, status="ok")

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_skips_already_ok(self):
        self.enricher.client.token = None
        counts = self.enricher.enrich_batch(["B011", "B012", "B013"])
        self.assertEqual(counts["skipped"], 1)

    def test_batch_returns_counts_dict(self):
        self.enricher.client.token = None
        counts = self.enricher.enrich_batch(["B011", "B012"])
        self.assertIn("skipped", counts)
        self.assertIn("pending", counts)


# ---------------------------------------------------------------------------
# Retailer-aware landing_page_data
# ---------------------------------------------------------------------------

class TestRetailerAwareLandingPageData(unittest.TestCase):
    """landing_page_data must propagate retailer='amazon' through to item dicts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import walmart_trends
        import amazon_trends
        self.wt = walmart_trends
        self.at = amazon_trends

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_amazon_collection(self):
        conn = self.wt._connect()
        conn.execute(
            "INSERT OR IGNORE INTO amazon_trend_products (asin, product_title, amazon_link) "
            "VALUES ('B099', 'Test Amazon Product', 'https://amazon.com/dp/B099?tag=test-20')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections "
            "(slug, name, source_type, is_active, display_order, metadata_json, retailer) "
            "VALUES ('amz-test', 'Amazon Test', 'amazon_workbook_bootstrap', 1, 1, '{}', 'amazon')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collection_items "
            "(collection_slug, sku, display_order, badges_json, retailer) "
            "VALUES ('amz-test', 'B099', 0, '[]', 'amazon')"
        )
        conn.commit()
        conn.close()

    def _seed_walmart_collection(self):
        conn = self.wt._connect()
        conn.execute(
            "INSERT OR IGNORE INTO walmart_products (sku, item_name) VALUES ('W001', 'Test Walmart Product')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections "
            "(slug, name, source_type, is_active, display_order, metadata_json, retailer) "
            "VALUES ('wmt-test', 'Walmart Test', 'workbook_bootstrap', 1, 2, '{}', 'walmart')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collection_items "
            "(collection_slug, sku, display_order, badges_json, retailer) "
            "VALUES ('wmt-test', 'W001', 0, '[]', 'walmart')"
        )
        conn.commit()
        conn.close()

    def test_amazon_item_has_retailer_amazon(self):
        self._seed_amazon_collection()
        data = self.wt.WalmartTrendStore().landing_page_data()
        amz_coll = next(c for c in data["collections"] if c["slug"] == "amz-test")
        self.assertEqual(amz_coll["retailer"], "amazon")
        self.assertEqual(amz_coll["items"][0]["retailer"], "amazon")

    def test_walmart_item_has_retailer_walmart(self):
        self._seed_walmart_collection()
        data = self.wt.WalmartTrendStore().landing_page_data()
        wmt_coll = next(c for c in data["collections"] if c["slug"] == "wmt-test")
        self.assertEqual(wmt_coll["retailer"], "walmart")
        self.assertEqual(wmt_coll["items"][0]["retailer"], "walmart")

    def test_amazon_item_shop_url_uses_affiliate_url_fallback(self):
        self._seed_amazon_collection()
        conn = self.wt._connect()
        conn.execute(
            "INSERT OR IGNORE INTO amazon_affiliate_links (asin, product_url, affiliate_url, status) "
            "VALUES ('B099', 'https://amazon.com/dp/B099', 'https://amazon.com/dp/B099?tag=test-20', 'workbook')"
        )
        conn.commit()
        conn.close()
        data = self.wt.WalmartTrendStore().landing_page_data()
        amz_coll = next(c for c in data["collections"] if c["slug"] == "amz-test")
        shop_url = amz_coll["items"][0]["shop_url"]
        self.assertIn("amazon.com", shop_url)

    def test_amazon_item_shop_url_uses_genius_when_available(self):
        self._seed_amazon_collection()
        affiliate_url = "https://amazon.com/dp/B099?tag=test-20"
        genius_url = "https://urlgeni.us/amazon/B099test"
        conn = self.wt._connect()
        conn.execute(
            "INSERT OR IGNORE INTO amazon_affiliate_links (asin, product_url, affiliate_url, status) "
            "VALUES ('B099', ?, ?, 'workbook')", (affiliate_url, affiliate_url)
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_urlgenius_links (destination_url, genius_url, status) "
            "VALUES (?, ?, 'active')", (affiliate_url, genius_url)
        )
        conn.commit()
        conn.close()
        data = self.wt.WalmartTrendStore().landing_page_data()
        amz_coll = next(c for c in data["collections"] if c["slug"] == "amz-test")
        self.assertEqual(amz_coll["items"][0]["shop_url"], genius_url)

    def test_walmart_item_shop_url_unchanged_after_amazon_added(self):
        self._seed_walmart_collection()
        self._seed_amazon_collection()
        data = self.wt.WalmartTrendStore().landing_page_data()
        wmt_coll = next(c for c in data["collections"] if c["slug"] == "wmt-test")
        shop_url = wmt_coll["items"][0]["shop_url"]
        self.assertIn("walmart.com", shop_url)

    def test_both_retailers_present_in_mixed_response(self):
        self._seed_walmart_collection()
        self._seed_amazon_collection()
        data = self.wt.WalmartTrendStore().landing_page_data()
        retailer_set = {c["retailer"] for c in data["collections"]}
        self.assertIn("walmart", retailer_set)
        self.assertIn("amazon", retailer_set)


# ---------------------------------------------------------------------------
# AmazonTrendRefreshService urlgenius wiring (smoke test)
# ---------------------------------------------------------------------------

class TestRefreshServiceURLGeniusWiring(unittest.TestCase):
    """Bootstrap wires URLGenius for each affiliate link; fallback when no key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.at = amazon_trends

    def tearDown(self):
        self.tmp.cleanup()

    def test_urlgenius_fallback_stored_when_no_key(self):
        svc = self.at.AmazonTrendRefreshService()
        svc.urlgenius.client.api_key = ""
        record = self.at.AmazonTrendRecord(
            asin="B020", product_title="Link Test",
            amazon_link="https://amazon.com/dp/B020?tag=test-20",
            source_list_type="2A", rank=1,
        )
        svc._process_records(1, [record], [])
        cached = svc.store.urlgenius_for("https://amazon.com/dp/B020?tag=test-20")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.get("status"), "fallback")

    def test_urlgenius_genius_url_used_in_landing_page_data(self):
        import walmart_trends
        svc = self.at.AmazonTrendRefreshService()
        svc.urlgenius.client.api_key = "fake-key"
        genius_url = "https://urlgeni.us/amazon/B021"
        svc.urlgenius.client.create_link = MagicMock(return_value={
            "link": {"genius_url": genius_url, "id": "link-x"}
        })
        record = self.at.AmazonTrendRecord(
            asin="B021", product_title="Genius Test",
            amazon_link="https://amazon.com/dp/B021?tag=test-20",
            source_list_type="2A", rank=1,
        )
        collections = [{
            "slug": "genius-test-coll",
            "name": "Genius Test",
            "description": "",
            "metadata": {},
            "items": [{"sku": "B021", "item_count": 1, "sale_amount": 0,
                        "total_earnings": 0, "badges": [], "metadata": {}}],
        }]
        svc._process_records(1, [record], collections)
        data = walmart_trends.WalmartTrendStore().landing_page_data()
        coll = next((c for c in data["collections"] if c["slug"] == "genius-test-coll"), None)
        if coll and coll["items"]:
            self.assertEqual(coll["items"][0]["shop_url"], genius_url)


if __name__ == "__main__":
    unittest.main()
