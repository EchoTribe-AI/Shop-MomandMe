"""Phase 3 coverage for the canonical UTM mapping on shop-side click-outs.

Plan §5 locks the shop UTM mapping to a single 5-tuple:

    utm_source   = platform           (default 'facebook')
    utm_medium   = medium             ('organic' for pages, 'agent' for chat)
    utm_campaign = collection_slug    (or 'shop' fallback)
    utm_content  = source_page        ('shop-landing', 'shop-directory',
                                       'shop-trends', 'agent-recommend', …)
    utm_term     = ASIN lowercased

The helper lives at ``link_builder.shop_utm``. Storefront chat builds its
own UTM dict inline (see storefront_chat.response_product, lines 467-477).
"""
from __future__ import annotations

import importlib
import inspect
import unittest


class ShopUtmHelperTest(unittest.TestCase):
    def setUp(self):
        import link_builder
        self.link_builder = importlib.reload(link_builder)

    def test_shop_utm_returns_canonical_five_tuple(self):
        utm = self.link_builder.shop_utm(
            "shop-landing", "my-coll", "B0ABC",
        )
        # Exactly these five keys — no more, no less.
        self.assertEqual(
            set(utm.keys()),
            {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"},
            "shop_utm must emit exactly the canonical 5-tuple from plan §5.",
        )

    def test_shop_utm_default_values(self):
        utm = self.link_builder.shop_utm(
            "shop-landing", "my-coll", "B0ABC",
        )
        self.assertEqual(utm["utm_source"], "facebook")
        self.assertEqual(utm["utm_medium"], "organic")
        self.assertEqual(utm["utm_campaign"], "my-coll")
        self.assertEqual(utm["utm_content"], "shop-landing")
        # ASIN must be lowercased.
        self.assertEqual(utm["utm_term"], "b0abc")

    def test_shop_utm_campaign_falls_back_to_shop_when_slug_empty(self):
        utm = self.link_builder.shop_utm("shop-directory", "", "B0XYZ")
        self.assertEqual(utm["utm_campaign"], "shop",
            "Missing collection_slug must fall back to 'shop', not ''")

    def test_shop_utm_campaign_falls_back_to_shop_when_slug_none(self):
        utm = self.link_builder.shop_utm("shop-directory", None, "B0XYZ")
        self.assertEqual(utm["utm_campaign"], "shop")

    def test_shop_utm_platform_and_medium_overrides(self):
        utm = self.link_builder.shop_utm(
            "agent-recommend", "kids-room", "B0CHAT123",
            platform="facebook", medium="agent",
        )
        self.assertEqual(utm["utm_source"], "facebook")
        self.assertEqual(utm["utm_medium"], "agent")
        self.assertEqual(utm["utm_content"], "agent-recommend")
        self.assertEqual(utm["utm_term"], "b0chat123")

    def test_shop_utm_term_lowercases_asin(self):
        utm = self.link_builder.shop_utm("shop-trends", "trends-page", "B0MIXEDcase")
        self.assertEqual(utm["utm_term"], "b0mixedcase",
            "utm_term must be ASIN lowercased per plan §5.")

    def test_shop_utm_handles_missing_asin(self):
        utm = self.link_builder.shop_utm("shop-landing", "x", None)
        self.assertEqual(utm["utm_term"], "")


class StorefrontChatUtmTest(unittest.TestCase):
    """Phase 1C aligned storefront_chat's UTM dict to the plan §5 contract.

    The helper builds UTMs inline inside ``response_product`` (around lines
    467-477). We verify the source carries the locked constants. This is a
    structural assertion, not a network-level test — the chat handler is
    deep inside an async pipeline and the rest of its behavior is covered
    by tests/test_storefront_chat.py.
    """

    def test_response_product_passes_agent_medium_and_agent_recommend_content(self):
        import storefront_chat
        src = inspect.getsource(storefront_chat.response_product)
        # utm_medium='agent' — NOT 'chat'. (plan §5 locks 'agent'.)
        self.assertIn('utm_medium="agent"', src,
            "response_product must build UTM with utm_medium='agent' "
            "per plan §5 (chat origin). 'chat' is the OLD value.")
        # utm_content='agent-recommend' — the canonical origin tag.
        self.assertIn('utm_content="agent-recommend"', src,
            "response_product must tag agent click-outs with "
            "utm_content='agent-recommend'.")

    def test_response_product_uses_facebook_source_and_term_lowercased(self):
        import storefront_chat
        src = inspect.getsource(storefront_chat.response_product)
        self.assertIn('utm_source="facebook"', src,
            "Agent clicks must declare utm_source='facebook' (plan §5).")
        # The ASIN-lowercased term contract.
        self.assertIn('utm_term=(item_id or "").lower()', src,
            "utm_term must be the item_id lowercased.")

    def test_response_product_utm_campaign_falls_back_to_agent(self):
        """When no current_slug is supplied, utm_campaign defaults to 'agent'."""
        import storefront_chat
        src = inspect.getsource(storefront_chat.response_product)
        # Either current_slug or 'agent' — the fallback wired into the call.
        self.assertIn('current_slug or "agent"', src,
            "utm_campaign must fall back to 'agent' when no slug is in play.")


if __name__ == "__main__":
    unittest.main()
