import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class StorefrontChatTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "storefront-chat.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        os.environ.pop("ANTHROPIC_API_KEY", None)

        import db_schema
        import product_api
        import app

        os.environ.pop("ANTHROPIC_API_KEY", None)
        db_schema.DB_PATH = self.db_path
        product_api.ArcherAPI.CACHE_DB = self.db_path
        db_schema.bootstrap()
        self.app_module = app
        self.client = app.app.test_client()
        self._seed_storefront_content()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_storefront_content(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO collages
                (slug, products_json, caption, creator_id, status, hero_title, click_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "baby-basics",
                    json.dumps([{
                        "asin": "BOTTLEBRUSH1",
                        "product_name": "Silicone Baby Bottle Brush",
                        "brand": "TinyHome",
                        "price": "9.99",
                        "network": "amazon",
                        "attribution_link": "https://www.amazon.com/dp/BOTTLEBRUSH1?tag=mommymedeals-20",
                    }]),
                    "Bottle cleaning basics",
                    "everydaywithsteph",
                    "published",
                    "Baby basics",
                    0,
                ),
            )
            conn.execute(
                """
                INSERT INTO collages
                (slug, products_json, caption, creator_id, status, hero_title, click_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "walmart-water-play",
                    json.dumps([{
                        "asin": "5454929532",
                        "product_name": "Walmart Backyard Splash Pad",
                        "brand": "PlayDay",
                        "price_display": "$18.00",
                        "network": "walmart",
                        "retailer": "Walmart",
                        "category": "Outdoor water toys",
                        "attribution_link": "https://goto.walmart.com/c/3590891/1398372/16662?u=water",
                        "item_count": 42,
                    }]),
                    "Summer water play finds",
                    "everydaywithsteph",
                    "published",
                    "Walmart water play",
                    3,
                ),
            )
            conn.execute(
                """
                INSERT INTO collages
                (slug, products_json, caption, creator_id, status, hero_title)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "draft-only",
                    json.dumps([{
                        "asin": "DRAFTONLY1",
                        "product_name": "Unpublished Secret Toy",
                        "network": "amazon",
                        "attribution_link": "https://www.amazon.com/dp/DRAFTONLY1?tag=mommymedeals-20",
                    }]),
                    "Should not appear",
                    "everydaywithsteph",
                    "draft",
                    "Draft only",
                ),
            )
            conn.execute(
                """
                INSERT INTO posts
                (creator_id, asin, network, angle, copy, collection_slug, status, smart_link,
                 product_name, product_brand, product_price, product_image, slug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "everydaywithsteph",
                    "B0TILEGIFT",
                    "amazon",
                    "toddler-gift",
                    "A toddler gift idea for quiet play: magnetic dinosaur tiles under $25.",
                    "",
                    "approved",
                    "https://urlgeni.us/amazon/tiles",
                    "Magnetic Dinosaur Tiles",
                    "Coodoo",
                    "$19.99",
                    "",
                    "toddler-gift-b0tilegift-1",
                ),
            )
            conn.execute(
                """
                INSERT INTO posts
                (creator_id, asin, network, angle, copy, status, smart_link,
                 product_name, product_brand, product_price, slug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "everydaywithsteph",
                    "DRAFTPOST1",
                    "amazon",
                    "draft",
                    "Draft post product",
                    "draft",
                    "https://urlgeni.us/amazon/draft",
                    "Draft Post Product",
                    "DraftBrand",
                    "$10.00",
                    "draft-post-1",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_chat_keeps_session_memory_for_follow_up(self):
        first = self.client.post("/api/shop/chat", json={
            "message": "I need toddler gift ideas",
            "creator_id": "everydaywithsteph",
            "slug": "baby-basics",
        }).get_json()
        self.assertTrue(first["session_id"])
        self.assertEqual(first["products"][0]["asin"], "B0TILEGIFT")

        second = self.client.post("/api/shop/chat", json={
            "message": "anything under $25?",
            "creator_id": "everydaywithsteph",
            "slug": "baby-basics",
            "session_id": first["session_id"],
        }).get_json()
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["products"][0]["asin"], "B0TILEGIFT")

        conn = sqlite3.connect(self.db_path)
        try:
            turns = json.loads(conn.execute(
                "SELECT turns_json FROM storefront_chat_sessions WHERE session_id = ?",
                (first["session_id"],),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(len(turns), 2)

    def test_chat_matches_published_collection_off_current_page_and_preserves_walmart_link(self):
        data = self.client.post("/api/shop/chat", json={
            "message": "show me splash pad water toys",
            "creator_id": "everydaywithsteph",
            "slug": "baby-basics",
        }).get_json()
        top = data["products"][0]
        self.assertEqual(top["asin"], "5454929532")
        self.assertEqual(top["retailer"], "Walmart")
        self.assertEqual(top["link"], "https://goto.walmart.com/c/3590891/1398372/16662?u=water")
        self.assertNotIn("amazon.com", top["link"])

    def test_chat_matches_published_post_and_excludes_drafts(self):
        data = self.client.post("/api/shop/chat", json={
            "message": "magnetic dinosaur tiles",
            "creator_id": "everydaywithsteph",
            "slug": "shop-home",
        }).get_json()
        product_names = [p["name"] for p in data["products"]]
        self.assertEqual(data["products"][0]["asin"], "B0TILEGIFT")
        self.assertIn("Magnetic Dinosaur Tiles", product_names)
        self.assertNotIn("Draft Post Product", product_names)
        self.assertEqual(data["products"][0]["link"], "https://urlgeni.us/amazon/tiles")

    def test_shop_subdomain_allows_public_chat_post(self):
        resp = self.client.post(
            "/api/shop/chat",
            json={
                "message": "splash pad",
                "creator_id": "everydaywithsteph",
                "slug": "walmart-water-play",
            },
            headers={"Host": "shop.echotribe.ai"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["products"][0]["asin"], "5454929532")

    def test_legacy_general_chat_route_remains_separate(self):
        with patch.object(self.app_module, "_get_chat_context", return_value=("prompt", [])):
            with patch.object(self.app_module.anthropic, "Anthropic") as anthropic_cls:
                anthropic_cls.return_value.messages.create.return_value.content = [
                    type("Text", (), {"text": "Legacy reply"})()
                ]
                resp = self.client.post("/api/chat", json={"message": "hello"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("session_id", resp.get_json())


if __name__ == "__main__":
    unittest.main()
