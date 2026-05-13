"""Unit tests for the Amazon Creators API client + enricher rewire."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from utils.amazon_creators import (
    AmazonCreatorsAPI,
    AmazonCreatorsConfigError,
    detect_credential_family,
    load_config,
    parse_item,
)


# --- response normalization ---


class ParseItemTests(unittest.TestCase):
    def test_full_shape_extracts_all_fields(self):
        item = {
            "asin": "B0TEST",
            "detailPageURL": "https://www.amazon.com/dp/B0TEST?tag=xyz-20&linkCode=ogi",
            "parentASIN": "B0PARENT",
            "images": {
                "primary": {
                    "large": {"url": "https://img/large.jpg"},
                    "medium": {"url": "https://img/medium.jpg"},
                }
            },
            "itemInfo": {
                "title": {"displayValue": "Test Product"},
                "byLineInfo": {
                    "brand": {"displayValue": "TestBrand"},
                    "manufacturer": {"displayValue": "TestMfg"},
                },
                "classifications": {
                    "productGroup": {"displayValue": "Electronics"},
                    "binding": {"displayValue": "Hardware"},
                },
            },
            "offersV2": {
                "listings": [
                    {
                        "isBuyBoxWinner": True,
                        "availability": {"type": "IN_STOCK", "message": "In Stock"},
                        "price": {
                            "money": {
                                "amount": 29.99,
                                "currency": "USD",
                                "displayAmount": "$29.99",
                            }
                        },
                    }
                ]
            },
        }
        parsed = parse_item(item)
        self.assertEqual(parsed["asin"], "B0TEST")
        self.assertEqual(parsed["product_title"], "Test Product")
        self.assertEqual(parsed["image_url"], "https://img/large.jpg")
        self.assertEqual(parsed["brand"], "TestBrand")
        self.assertEqual(parsed["category"], "Electronics")
        self.assertEqual(parsed["current_price"], 29.99)
        self.assertEqual(parsed["price_display"], "$29.99")
        self.assertEqual(parsed["availability_type"], "IN_STOCK")
        self.assertEqual(parsed["parent_asin"], "B0PARENT")
        self.assertEqual(
            parsed["detail_page_url"],
            "https://www.amazon.com/dp/B0TEST?tag=xyz-20&linkCode=ogi",
        )

    def test_falls_back_from_large_to_medium_image(self):
        item = {
            "asin": "B0A",
            "images": {"primary": {"large": None, "medium": {"url": "https://m.jpg"}}},
        }
        self.assertEqual(parse_item(item)["image_url"], "https://m.jpg")

    def test_missing_asin_returns_empty(self):
        self.assertEqual(parse_item({"foo": "bar"}), {})


class CredentialFamilyTests(unittest.TestCase):
    def test_v2(self):
        self.assertEqual(detect_credential_family("2.1"), "v2.x")

    def test_v3(self):
        self.assertEqual(detect_credential_family("3.1"), "v3.x")

    def test_unknown_raises(self):
        with self.assertRaises(AmazonCreatorsConfigError):
            detect_credential_family("99")


class GetItemsHttpTests(unittest.TestCase):
    def setUp(self):
        self.client = AmazonCreatorsAPI(
            {
                "client_id": "id",
                "client_secret": "secret",
                "version": "3.1",
                "partner_tag": "tag-20",
                "marketplace": "www.amazon.com",
            }
        )
        # Pretend we already have a fresh token.
        self.client._token = "fake-token"
        self.client._token_expires_at = 9999999999.0

    @patch("utils.amazon_creators.requests.post")
    def test_get_items_sends_required_payload_and_parses(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "itemsResult": {
                    "items": [
                        {
                            "asin": "B0X",
                            "detailPageURL": "https://www.amazon.com/dp/B0X?tag=tag-20",
                            "images": {"primary": {"large": {"url": "https://i/l.jpg"}}},
                            "itemInfo": {"title": {"displayValue": "X"}},
                            "offersV2": {
                                "listings": [
                                    {
                                        "availability": {"type": "IN_STOCK"},
                                        "price": {
                                            "money": {
                                                "amount": 9.99,
                                                "currency": "USD",
                                                "displayAmount": "$9.99",
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        )
        out = self.client.get_items(["B0X"])
        self.assertIn("B0X", out)
        self.assertEqual(out["B0X"]["price_display"], "$9.99")
        # Verify body/headers contained the required fields.
        _, kwargs = mock_post.call_args
        body = kwargs["data"]
        self.assertIn('"itemIdType": "ASIN"', body)
        self.assertIn('"partnerTag": "tag-20"', body)
        self.assertIn('"marketplace": "www.amazon.com"', body)
        self.assertIn("images.primary.large", body)
        self.assertIn("images.primary.medium", body)
        self.assertIn("offersV2.listings.price", body)
        self.assertIn("parentASIN", body)
        self.assertEqual(kwargs["headers"]["x-marketplace"], "www.amazon.com")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake-token")

    @patch("utils.amazon_creators.requests.post")
    def test_batches_of_10(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"itemsResult": {"items": []}}
        )
        self.client.get_items([f"A{i:02d}" for i in range(23)])
        self.assertEqual(mock_post.call_count, 3)  # 10 + 10 + 3


class ConfigLoadingTests(unittest.TestCase):
    def test_replit_secret_names_are_picked_up(self):
        env = {
            "CREDENTIAL_ID": "from-replit",
            "CREDENTIAL_SECRET": "shh",
            "AMAZON_AFFILIATE_TAG": "tag-20",
        }
        with patch.dict(os.environ, env, clear=False):
            for k in (
                "AMAZON_CREATORS_CLIENT_ID",
                "AMAZON_CREATORS_CLIENT_SECRET",
                "AMAZON_PARTNER_TAG",
                "AMAZON_CREATORS_CREDENTIAL_VERSION",
                "AMAZON_MARKETPLACE",
            ):
                os.environ.pop(k, None)
            cfg = load_config()
            self.assertEqual(cfg["client_id"], "from-replit")
            self.assertEqual(cfg["client_secret"], "shh")
            self.assertEqual(cfg["partner_tag"], "tag-20")
            self.assertEqual(cfg["version"], "3.1")
            self.assertEqual(cfg["marketplace"], "www.amazon.com")


# --- enricher integration with Creators primary + Crawlbase fallback ---


class EnricherRoutingTests(unittest.TestCase):
    def setUp(self):
        # Isolated DB per test.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        os.environ["CACHE_DB_PATH"] = self._tmp.name
        import importlib
        import db_schema
        import walmart_trends
        import amazon_trends
        importlib.reload(db_schema)
        importlib.reload(walmart_trends)
        importlib.reload(amazon_trends)
        self.amazon_trends = amazon_trends
        db_schema.bootstrap()
        self.store = amazon_trends.AmazonTrendStore()
        # Seed two ASINs.
        self.store.upsert_product(amazon_trends.AmazonTrendRecord(asin="A1"))
        self.store.upsert_product(amazon_trends.AmazonTrendRecord(asin="A2"))

    def tearDown(self):
        os.unlink(self._tmp.name)
        os.environ.pop("CACHE_DB_PATH", None)

    def test_creators_primary_then_crawlbase_fallback(self):
        creators = MagicMock()
        creators.configured = True
        creators.get_items.return_value = {
            "A1": {
                "asin": "A1",
                "product_title": "Title A1",
                "image_url": "https://img/a1.jpg",
                "price_display": "$10.00",
                "current_price": 10.0,
                "availability_type": "IN_STOCK",
                "parent_asin": "PA1",
                "detail_page_url": "https://www.amazon.com/dp/A1?tag=x",
            }
        }
        fallback = MagicMock()
        fallback.token = "stub"
        fallback.get_amazon_product.return_value = {
            "image_url": "https://img/a2.jpg",
            "current_price": 12.5,
            "price_display": "$12.50",
            "brand": "B",
        }
        enricher = self.amazon_trends.AmazonProductEnricher(
            self.store, creators=creators, fallback=fallback
        )
        counts = enricher.enrich_batch(["A1", "A2"], max_workers=2)
        self.assertEqual(counts["creators"], 1)
        self.assertEqual(counts["crawlbase"], 1)
        a1 = self.store.get_product("A1")
        self.assertEqual(a1["detail_page_url"], "https://www.amazon.com/dp/A1?tag=x")
        self.assertEqual(a1["availability_type"], "IN_STOCK")
        self.assertEqual(a1["parent_asin"], "PA1")
        a2 = self.store.get_product("A2")
        self.assertEqual(a2["price_display"], "$12.50")
        self.assertEqual(a2["image_url"], "https://img/a2.jpg")

    def test_creators_unconfigured_falls_back_for_all(self):
        creators = MagicMock()
        creators.configured = False
        fallback = MagicMock()
        fallback.token = "stub"
        fallback.get_amazon_product.return_value = {
            "image_url": "https://img/x.jpg",
            "current_price": 1.0,
            "price_display": "$1.00",
        }
        enricher = self.amazon_trends.AmazonProductEnricher(
            self.store, creators=creators, fallback=fallback
        )
        counts = enricher.enrich_batch(["A1"], max_workers=1)
        creators.get_items.assert_not_called()
        self.assertEqual(counts["crawlbase"], 1)


if __name__ == "__main__":
    unittest.main()
