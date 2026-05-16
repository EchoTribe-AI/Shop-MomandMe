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

    # --- product_title regression (Issue 2: enrichment said "30 updated"
    # but cards still showed "Amazon find") --------------------------------

    def test_update_writes_product_title_from_enrichment_payload(self):
        # Without this write, cards in /trends fall back to the literal
        # "Amazon find" placeholder forever, even after enrichment runs.
        self.store.update_product_enrichment(
            "B001",
            {"product_title": "Live Title From Creators API"},
            status="ok",
        )
        product = self.store.get_product("B001")
        self.assertEqual(product["product_title"], "Live Title From Creators API")

    def test_update_preserves_existing_title_when_payload_is_blank(self):
        # Enrichment runs that don't have a title (rare but possible) must
        # NOT clobber a populated workbook title with an empty string.
        self.store.update_product_enrichment(
            "B001",
            {"product_title": "Real Title"},
            status="ok",
        )
        self.store.update_product_enrichment(
            "B001",
            {"product_title": ""},  # empty — should preserve
            status="ok",
        )
        product = self.store.get_product("B001")
        self.assertEqual(product["product_title"], "Real Title")

    def test_update_returns_rowcount_one_for_existing_asin(self):
        # Rowcount visibility lets the admin enrich button distinguish
        # "30 real updates" from "30 successful no-ops on missing rows".
        rows = self.store.update_product_enrichment(
            "B001", {"image_url": "https://x.com/a.jpg"}, status="ok",
        )
        self.assertEqual(rows, 1)

    def test_update_returns_rowcount_zero_when_asin_does_not_exist(self):
        # Missing-row case — surfaces the "import never inserted this ASIN"
        # data gap that previously hid behind the success counter.
        rows = self.store.update_product_enrichment(
            "B999_DOES_NOT_EXIST",
            {"image_url": "https://x.com/a.jpg"},
            status="ok",
        )
        self.assertEqual(rows, 0)


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

    def test_enrich_via_crawlbase_writes_product_title_from_name_field(self):
        # Crawlbase typically returns `name` (sometimes `title`). The mapping
        # in _enrich_via_crawlbase must lift that into the data dict so the
        # PG row gets product_title populated. Same regression class as the
        # Creators-side fix: without this, cards render "Amazon find" even
        # after enrichment.
        self.enricher.client.token = "fake-token"
        self.enricher.client.get_amazon_product = MagicMock(return_value={
            "name": "Crawlbase Scraped Title",
            "imageUrl": "https://img.amazon.com/b010.jpg",
            "price": "9.99",
        })
        self.enricher.enrich("B010")
        product = self.store.get_product("B010")
        self.assertEqual(product["product_title"], "Crawlbase Scraped Title")
        self.assertEqual(product["enrichment_status"], "ok")

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

    def test_batch_counts_include_missing_rows_field(self):
        # The missing_rows counter is what surfaces "the API said success
        # but the ASIN didn't exist in PG" — the class of bug that made
        # /admin/amazon-trends/enrich report "30 updated" while the page
        # still showed defaults.
        self.enricher.client.token = None
        counts = self.enricher.enrich_batch(["B012"])
        self.assertIn("missing_rows", counts)
        self.assertEqual(counts["missing_rows"], 0)


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

    def test_amazon_item_has_widget_fallback_when_image_missing(self):
        self._seed_amazon_collection()
        data = self.wt.WalmartTrendStore().landing_page_data()
        amz_coll = next(c for c in data["collections"] if c["slug"] == "amz-test")
        item = amz_coll["items"][0]
        self.assertEqual(item["image_url"], "")
        self.assertIn("ws-na.amazon-adsystem.com/widgets/q", item["fallback_image_url"])
        self.assertIn("ASIN=B099", item["fallback_image_url"])
        self.assertEqual(item["price_display"], "")

    def test_amazon_item_uses_stored_image_and_price_when_present(self):
        self._seed_amazon_collection()
        conn = self.wt._connect()
        conn.execute(
            """
            UPDATE amazon_trend_products
            SET image_url = ?, current_price = ?, price_display = ?
            WHERE asin = ?
            """,
            ("https://images.example/b099.jpg", 19.99, "$19.99", "B099"),
        )
        conn.commit()
        conn.close()
        data = self.wt.WalmartTrendStore().landing_page_data()
        item = next(c for c in data["collections"] if c["slug"] == "amz-test")["items"][0]
        self.assertEqual(item["image_url"], "https://images.example/b099.jpg")
        self.assertEqual(item["price_display"], "$19.99")

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

    def test_landing_page_data_uses_bounded_queries_for_many_items(self):
        conn = self.wt._connect()
        for i in range(12):
            sku = f"W{i:03d}"
            conn.execute(
                "INSERT OR IGNORE INTO walmart_products (sku, item_name, image_url, price_display) VALUES (?, ?, ?, ?)",
                (sku, f"Walmart Product {i}", f"https://img.example/{sku}.jpg", "$9.99"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO walmart_affiliate_links (sku, product_url, impact_url, status) VALUES (?, ?, ?, 'active')",
                (sku, f"https://www.walmart.com/ip/{sku}", f"https://goto.walmart.com/{sku}"),
            )
        for i in range(12):
            asin = f"B{i:09d}"
            conn.execute(
                "INSERT OR IGNORE INTO amazon_trend_products (asin, product_title, amazon_link) VALUES (?, ?, ?)",
                (asin, f"Amazon Product {i}", f"https://amazon.com/dp/{asin}?tag=test-20"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO amazon_affiliate_links (asin, product_url, affiliate_url, status) VALUES (?, ?, ?, 'workbook')",
                (asin, f"https://amazon.com/dp/{asin}", f"https://amazon.com/dp/{asin}?tag=test-20"),
            )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections (slug, name, source_type, is_active, display_order, metadata_json, retailer) VALUES ('bulk-wmt', 'Bulk Walmart', 'workbook_bootstrap', 1, 1, '{}', 'walmart')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections (slug, name, source_type, is_active, display_order, metadata_json, retailer) VALUES ('bulk-amz', 'Bulk Amazon', 'amazon_workbook_bootstrap', 1, 2, '{}', 'amazon')"
        )
        for i in range(12):
            conn.execute(
                "INSERT OR REPLACE INTO walmart_collection_items (collection_slug, sku, display_order, badges_json, retailer) VALUES ('bulk-wmt', ?, ?, '[]', 'walmart')",
                (f"W{i:03d}", i),
            )
            conn.execute(
                "INSERT OR REPLACE INTO walmart_collection_items (collection_slug, sku, display_order, badges_json, retailer) VALUES ('bulk-amz', ?, ?, '[]', 'amazon')",
                (f"B{i:09d}", i),
            )
        conn.commit()
        conn.close()

        original_connect = self.wt._connect
        query_count = {"n": 0}

        class CountingConn:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, *args, **kwargs):
                query_count["n"] += 1
                return self.inner.execute(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        with patch.object(self.wt, "_connect", side_effect=lambda: CountingConn(original_connect())):
            data = self.wt.WalmartTrendStore().landing_page_data()

        self.assertGreaterEqual(sum(len(c["items"]) for c in data["collections"]), 24)
        self.assertLessEqual(query_count["n"], 8)


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


class TestEnrichmentDecoupledFromBootstrap(unittest.TestCase):
    """Bootstrap must not block on Crawlbase enrichment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.at = amazon_trends

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("CACHE_DB_PATH",):
            os.environ.pop(k, None)

    def test_bootstrap_does_not_call_enricher(self):
        """If bootstrap called enrich_batch, slow Crawlbase would block import."""
        svc = self.at.AmazonTrendRefreshService()
        svc.enricher.enrich_batch = MagicMock(return_value={})
        record = self.at.AmazonTrendRecord(
            asin="B099", product_title="Fast Import",
            amazon_link="https://amazon.com/dp/B099?tag=t-20",
            source_list_type="2A", rank=1,
        )
        collections = [{
            "slug": "fast-coll", "name": "Fast", "description": "",
            "metadata": {}, "items": [{"sku": "B099", "item_count": 1,
                "sale_amount": 0, "total_earnings": 0, "badges": [], "metadata": {}}],
        }]
        svc._process_records(1, [record], collections)
        svc.enricher.enrich_batch.assert_not_called()


class TestPendingPrioritization(unittest.TestCase):
    """pending_asins_prioritized returns active-collection ASINs first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        _setup_db(self.db_path)
        import amazon_trends
        self.at = amazon_trends
        self.store = amazon_trends.AmazonTrendStore()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("CACHE_DB_PATH", None)

    def test_active_collection_asin_ranks_above_orphan(self):
        # Both ASINs pending, only B_ACTIVE is in an active collection
        for asin in ("B_ORPHAN", "B_ACTIVE"):
            self.store.upsert_product(self.at.AmazonTrendRecord(
                asin=asin, product_title=asin, amazon_link=f"https://amazon.com/dp/{asin}",
                source_list_type="2A", rank=1,
            ))
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO walmart_collections (slug, name, source_type, retailer, is_active) "
            "VALUES (?, ?, 'amazon_workbook_curated', 'amazon', 1)",
            ("test-coll", "Test"),
        )
        conn.execute(
            "INSERT INTO walmart_collection_items (collection_slug, sku, retailer) "
            "VALUES (?, ?, 'amazon')",
            ("test-coll", "B_ACTIVE"),
        )
        conn.commit()
        conn.close()
        result = self.store.pending_asins_prioritized(limit=10)
        self.assertIn("B_ACTIVE", result)
        self.assertIn("B_ORPHAN", result)
        self.assertEqual(result[0], "B_ACTIVE")

    def test_already_ok_with_image_is_excluded(self):
        self.store.upsert_product(self.at.AmazonTrendRecord(
            asin="B_DONE", product_title="Done", amazon_link="",
            source_list_type="2A", rank=1,
        ))
        self.store.update_product_enrichment(
            "B_DONE",
            {"image_url": "https://x/img.jpg", "current_price": 9.99, "price_display": "$9.99"},
            status="ok",
        )
        result = self.store.pending_asins_prioritized(limit=10)
        self.assertNotIn("B_DONE", result)

    def test_enrich_pending_returns_counts_with_queued(self):
        self.store.upsert_product(self.at.AmazonTrendRecord(
            asin="B_PEND", product_title="P", amazon_link="",
            source_list_type="2A", rank=1,
        ))
        svc = self.at.AmazonTrendRefreshService()
        svc.enricher.client.token = None  # force skip path
        counts = svc.enrich_pending(limit=5, max_workers=2)
        self.assertIn("queued", counts)
        self.assertEqual(counts["queued"], 1)


class TestCrawlbaseAmazonContract(unittest.TestCase):
    """Verify spec-compliant Crawlbase Amazon enrichment per Amazon_Crawlbase_URLGenius_Spec."""

    def test_request_uses_lightweight_shape_and_90s_timeout(self):
        from product_api import CrawlbaseAPI
        api = CrawlbaseAPI()
        api.token = "fake-token"
        captured: dict = {}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["timeout"] = timeout
            resp = MagicMock()
            resp.status_code = 200
            resp.text = '<html><span id="productTitle">X</span></html>'
            resp.raise_for_status = lambda: None
            return resp

        with patch("product_api.requests.get", side_effect=fake_get):
            api.get_amazon_product("B0TEST")

        self.assertEqual(captured["url"], "https://api.crawlbase.com/")
        self.assertEqual(captured["params"]["token"], "fake-token")
        self.assertEqual(captured["params"]["ajax_wait"], "true")
        self.assertEqual(captured["params"]["page_wait"], "2000")
        self.assertIn("/dp/B0TEST", captured["params"]["url"])
        self.assertEqual(captured["timeout"], 90)

    def test_parses_title_image_price_brand(self):
        from product_api import CrawlbaseAPI
        api = CrawlbaseAPI()
        html = (
            '<html>'
            '<span id="productTitle">Cool Widget XL</span>'
            '<img id="landingImage" src="https://images-na.amazon.com/widget.jpg" />'
            '<span class="a-price-whole">29</span><span class="a-price-fraction">99</span>'
            '<a id="bylineInfo">Visit the WidgetCo Store</a>'
            '</html>'
        )
        result = api._parse_amazon_product(html, "B0TEST")
        self.assertIsNotNone(result)
        self.assertEqual(result["product_title"], "Cool Widget XL")
        self.assertEqual(result["image_url"], "https://images-na.amazon.com/widget.jpg")
        self.assertEqual(result["current_price"], 29.99)
        self.assertEqual(result["price_display"], "$29.99")
        self.assertEqual(result["brand"], "WidgetCo")

    def test_parser_returns_none_for_empty_html(self):
        from product_api import CrawlbaseAPI
        api = CrawlbaseAPI()
        self.assertIsNone(api._parse_amazon_product("<html></html>", "B0TEST"))


if __name__ == "__main__":
    unittest.main()
