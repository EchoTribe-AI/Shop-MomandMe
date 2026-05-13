"""Unit tests for the Amazon Creators API client + enricher rewire."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from utils.amazon_creators import (
    AmazonCreatorsAPI,
    AmazonCreatorsAPIError,
    AmazonCreatorsConfigError,
    AmazonCreatorsFatalError,
    detect_credential_family,
    load_config,
    parse_item,
)
import utils.amazon_creators as creators_mod


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
        # Disable pacing for this test so it runs fast.
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            self.client.get_items([f"A{i:02d}" for i in range(23)])
        self.assertEqual(mock_post.call_count, 3)  # 10 + 10 + 3


class RateLimitGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.client = AmazonCreatorsAPI(
            {
                "client_id": "id", "client_secret": "secret", "version": "3.1",
                "partner_tag": "tag-20", "marketplace": "www.amazon.com",
            }
        )
        self.client._token = "fake-token"
        self.client._token_expires_at = 9999999999.0

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_pacing_enforced_between_batches(self, mock_post, mock_sleep):
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"itemsResult": {"items": []}}
        )
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 1.05):
            self.client.get_items([f"A{i:02d}" for i in range(15)])
        # 2 batches → at least one paced sleep call before batch 2.
        sleep_durations = [c.args[0] for c in mock_sleep.call_args_list if c.args]
        self.assertTrue(any(0.5 < d <= 1.05 for d in sleep_durations),
                        f"Expected a pacing sleep ~1s, got {sleep_durations}")

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_throttle_429_retries_then_succeeds(self, mock_post, mock_sleep):
        ok = MagicMock(status_code=200, json=lambda: {
            "itemsResult": {"items": [{"asin": "B0X"}]}
        })
        throttled = MagicMock(
            status_code=429,
            text=json.dumps({
                "type": "ThrottleException",
                "message": "rate limited",
                "retryAfterSeconds": 2,
            }),
        )
        mock_post.side_effect = [throttled, throttled, ok]
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            out = self.client.get_items(["B0X"])
        self.assertIn("B0X", out)
        self.assertEqual(mock_post.call_count, 3)
        # The retry-after value (2) should be honored.
        self.assertIn(2, [c.args[0] for c in mock_sleep.call_args_list if c.args])

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_throttle_429_gives_up_after_max_retries(self, mock_post, mock_sleep):
        throttled = MagicMock(
            status_code=429,
            text=json.dumps({"type": "ThrottleException", "message": "x"}),
        )
        mock_post.return_value = throttled
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            with patch.object(creators_mod, "MAX_RETRIES_ON_THROTTLE", 2):
                out = self.client.get_items(["B0X"])
        self.assertEqual(out, {})
        # initial + 2 retries = 3
        self.assertEqual(mock_post.call_count, 3)

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_invalid_partner_tag_raises_fatal_and_stops(self, mock_post, mock_sleep):
        mock_post.return_value = MagicMock(
            status_code=400,
            text=json.dumps({
                "type": "ValidationException",
                "message": "Partner tag in the request is invalid",
                "reason": "InvalidPartnerTag",
            }),
        )
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            with self.assertRaises(AmazonCreatorsFatalError) as cm:
                self.client.get_items(["B0X"])
        self.assertEqual(cm.exception.reason, "InvalidPartnerTag")
        self.assertEqual(mock_post.call_count, 1)  # no retries on fatal

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_associate_not_eligible_raises_fatal(self, mock_post, mock_sleep):
        mock_post.return_value = MagicMock(
            status_code=403,
            text=json.dumps({
                "type": "AccessDeniedException",
                "message": "ineligible",
                "reason": "AssociateNotEligible",
            }),
        )
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            with self.assertRaises(AmazonCreatorsFatalError) as cm:
                self.client.get_items(["B0X"])
        self.assertEqual(cm.exception.reason, "AssociateNotEligible")

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_resource_not_found_returns_empty_no_raise(self, mock_post, mock_sleep):
        mock_post.return_value = MagicMock(
            status_code=404,
            text=json.dumps({
                "type": "ResourceNotFoundException",
                "message": "No items found",
                "resourceType": "Item",
                "resourceId": "B0BAD",
            }),
        )
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            out = self.client.get_items(["B0BAD"])
        self.assertEqual(out, {})

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_token_expired_refreshes_once_then_succeeds(self, mock_post, mock_sleep):
        expired = MagicMock(
            status_code=401,
            text=json.dumps({
                "type": "UnauthorizedException",
                "message": "expired",
                "reason": "TokenExpired",
            }),
        )
        ok = MagicMock(status_code=200, json=lambda: {"itemsResult": {"items": [{"asin": "B0X"}]}})
        mock_post.side_effect = [expired, ok]
        # Stub _fetch_token to avoid hitting real auth endpoint.
        self.client._fetch_token = MagicMock(return_value=("new-token", 3600))
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            out = self.client.get_items(["B0X"])
        self.assertIn("B0X", out)
        self.assertEqual(mock_post.call_count, 2)

    @patch("utils.amazon_creators.time.sleep")
    @patch("utils.amazon_creators.requests.post")
    def test_max_batches_per_run_caps_tpd(self, mock_post, mock_sleep):
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"itemsResult": {"items": []}}
        )
        with patch.object(creators_mod, "MIN_REQUEST_INTERVAL_SEC", 0.0):
            with patch.object(creators_mod, "MAX_BATCHES_PER_RUN", 2):
                # 35 ASINs → 4 batches in principle, but cap stops at 2.
                self.client.get_items([f"A{i:02d}" for i in range(35)])
        self.assertEqual(mock_post.call_count, 2)


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

    def test_fatal_error_aborts_run_no_crawlbase_fallback(self):
        creators = MagicMock()
        creators.configured = True
        creators.get_items.side_effect = AmazonCreatorsFatalError(
            "InvalidPartnerTag", "bad tag", 400
        )
        fallback = MagicMock()
        fallback.token = "stub"
        enricher = self.amazon_trends.AmazonProductEnricher(
            self.store, creators=creators, fallback=fallback
        )
        counts = enricher.enrich_batch(["A1", "A2"], max_workers=1)
        self.assertEqual(counts.get("fatal"), 1)
        self.assertEqual(counts.get("fatal_reason"), "InvalidPartnerTag")
        # Crawlbase should NOT have been called — fixing the config is the
        # only correct action when the partner tag is wrong.
        fallback.get_amazon_product.assert_not_called()

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
