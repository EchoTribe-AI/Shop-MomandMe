import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


def _walmart_collection(count=12):
    return {
        "slug": "kids-room-character-favorites",
        "name": "Kids Room + Character Favorites",
        "description": "Character room finds",
        "items": [
            {
                "sku": f"WM{i:03d}",
                "title": f"Character Sheet Set {i}",
                "brand": "WalmartBrand",
                "price_display": "$10.00",
                "image_url": f"https://i.example/{i}.jpg",
                "shop_url": f"https://goto.walmart.com/c/3590891/1398372/16662?u=wm{i}",
                "category": "Kids room",
                "rank": i,
            }
            for i in range(1, count + 1)
        ],
    }


def _live_item(sku):
    return {
        "sku": sku,
        "name": f"Live {sku}",
        "brand": "LiveBrand",
        "price": "7.77",
        "price_display": "$7.77",
        "imageUrl": f"https://live.example/{sku}.jpg",
        "url": f"https://www.walmart.com/ip/{sku}",
        "category": "Live category",
        "availability": "In stock",
        "rating": 4.7,
        "review_count": 321,
    }


class WalmartStorefrontCleanupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "storefront-cleanup.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self._admin_env = {
            key: os.environ.pop(key, None)
            for key in ("WALMART_TRENDS_ADMIN_TOKEN", "ADMIN_API_TOKEN", "ADMIN_SECRET")
        }

        import db_schema
        import collection_service
        import collection_content
        import product_api
        import app

        db_schema.DB_PATH = self.db_path
        collection_service.db_schema.DB_PATH = self.db_path
        collection_content.db_schema.DB_PATH = self.db_path
        product_api.ArcherAPI.CACHE_DB = self.db_path
        db_schema.bootstrap()
        self.db_schema = db_schema
        self.collection_content = collection_content
        self.app_module = app
        self.client = app.app.test_client()

    def tearDown(self):
        for key, value in self._admin_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def _json_count(self, table, column, where_col, value):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                f"SELECT json_array_length({column}) FROM {table} WHERE {where_col} = ?",
                (value,),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_walmart_collection_preview_and_publish_preserve_full_source_count_and_links(self):
        source = _walmart_collection(12)
        original_link = source["items"][0]["shop_url"]
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            with patch("product_api.WalmartAPI.get_item_by_id", side_effect=_live_item):
                draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                    "creator_id": "everydaywithsteph",
                    "public_slug": "walmart-kids-room-character-favorites",
                    "title": "Kids Room + Character Favorites",
                    "landing_intro": "Fresh finds.",
                })
        self.assertEqual(draft_resp.status_code, 200)
        draft = draft_resp.get_json()["draft"]
        self.assertEqual(len(source["items"]), 12)
        self.assertEqual(len(draft["product_snapshot"]), 12)
        self.assertEqual(self._json_count("collages", "products_json", "slug", "walmart-kids-room-character-favorites"), 12)

        publish_resp = self.client.post(f"/api/collection-content-drafts/{draft['id']}/publish")
        self.assertEqual(publish_resp.status_code, 200)
        self.assertEqual(self._json_count("collages", "products_json", "slug", "walmart-kids-room-character-favorites"), 12)

        conn = sqlite3.connect(self.db_path)
        try:
            products = json.loads(conn.execute(
                "SELECT products_json FROM collages WHERE slug = ?",
                ("walmart-kids-room-character-favorites",),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(products[0]["attribution_link"], original_link)
        self.assertEqual(products[0]["price_display"], "$7.77")
        self.assertEqual(products[0]["availability"], "In stock")
        self.assertEqual(products[0]["rating"], 4.7)
        self.assertEqual(products[0]["review_count"], 321)

    def test_create_post_ui_can_preview_subset_while_showing_full_count(self):
        with patch.object(self.collection_content, "get_walmart_collection", return_value=_walmart_collection(12)):
            resp = self.client.get("/collections/kids-room-character-favorites/create-post")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Showing 10 of 12 products", html)

    def test_legacy_walmart_routes_redirect_to_canonical_collections_routes(self):
        with patch.object(self.collection_content, "get_walmart_collection", return_value=_walmart_collection(1)):
            resp = self.client.get(
                "/walmart/collections/kids-room-character-favorites/create-post"
                "?creator_id=everydaywithsteph",
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(
            "/collections/kids-room-character-favorites/create-post",
            resp.headers.get("Location", ""),
        )
        self.assertIn("creator_id=everydaywithsteph", resp.headers.get("Location", ""))

    def test_public_nav_and_trends_route_render_on_shop_pages(self):
        source = _walmart_collection(1)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "landing_intro": "Fresh finds.",
            })
        landing = self.client.get("/shop/walmart-kids-room-character-favorites?preview=1").get_data(as_text=True)
        posts = self.client.get("/shop/posts").get_data(as_text=True)
        directory = self.client.get("/shop/").get_data(as_text=True)

        for html in (landing, posts, directory):
            self.assertIn("https://shop.echotribe.ai/collections", html)
            self.assertIn("https://shop.echotribe.ai/trends", html)
            self.assertIn("https://shop.echotribe.ai/posts", html)

        collections = self.client.get("/collections", headers={"Host": "shop.echotribe.ai"})
        self.assertEqual(collections.status_code, 200)
        self.assertIn("Shop creator-curated collections", collections.get_data(as_text=True))

        with patch("walmart_trends.get_trending_page_data", return_value={"last_refreshed": "Today", "collections": []}):
            trends = self.client.get("/trends", headers={"Host": "shop.echotribe.ai"})
        self.assertEqual(trends.status_code, 200)
        self.assertIn("What’s Trending Now", trends.get_data(as_text=True))

    def test_walmart_origin_pages_use_walmart_editor_not_six_slot_collage_editor(self):
        source = _walmart_collection(12)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "creator_id": "everydaywithsteph",
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Kids Room + Character Favorites",
                "landing_intro": "Original rich landing intro",
                "social_post": "Original social post",
                "hooks": [{"type": "Fast Discovery", "text": "Original hook"}],
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        publish_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/publish")
        self.assertEqual(publish_resp.status_code, 200)

        collage_resp = self.client.get("/archer/collage/walmart-kids-room-character-favorites")
        self.assertEqual(collage_resp.status_code, 200)
        collage = collage_resp.get_json()["collage"]
        self.assertEqual(collage["editor_type"], "trend_collection")
        self.assertEqual(collage["edit_url"], "/collections/walmart-kids-room-character-favorites/edit")
        self.assertEqual(len(collage["products"]), 12)

        generic_save = self.client.post("/archer/collage/save", json={
            "slug": "walmart-kids-room-character-favorites",
            "products": [{"asin": "B000000001", "product_name": "Flattened"}],
            "caption": "Generic caption",
            "status": "published",
        })
        self.assertEqual(generic_save.status_code, 409)

        editor = self.client.get("/collections/walmart-kids-room-character-favorites/edit")
        self.assertEqual(editor.status_code, 200)
        html = editor.get_data(as_text=True)
        self.assertIn("Walmart page editor", html)
        self.assertIn("Showing 10 of 12 products", html)
        self.assertIn("Original rich landing intro", html)
        self.assertIn("Original social post", html)
        self.assertIn("Original hook", html)

        update_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
            "draft_id": draft_id,
            "creator_id": "everydaywithsteph",
            "public_slug": "walmart-kids-room-character-favorites",
            "title": "Kids Room + Character Favorites Updated",
            "landing_intro": "Updated intro without flattening",
            "social_post": "Updated social post",
            "hooks": [{"type": "Fast Discovery", "text": "Updated hook"}],
        })
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(len(update_resp.get_json()["draft"]["product_snapshot"]), 12)
        self.assertEqual(self._json_count("collages", "products_json", "slug", "walmart-kids-room-character-favorites"), 12)

    def test_backfill_enriches_walmart_posts_without_rewriting_links(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO posts
                (creator_id, asin, network, angle, copy, status, smart_link,
                 product_name, product_brand, product_price, product_image, slug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "everydaywithsteph",
                    "WMPOST1",
                    "walmart",
                    "kid-room",
                    "Walmart post",
                    "approved",
                    "https://goto.walmart.com/c/3590891/1398372/16662?u=post",
                    "Old title",
                    "",
                    "",
                    "",
                    "kid-room-wmpost1",
                ),
            )
            post_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        os.environ["WALMART_TRENDS_ADMIN_TOKEN"] = "test-token"
        try:
            with patch("product_api.WalmartAPI.get_item_by_id", side_effect=_live_item):
                resp = self.client.post(
                    "/admin/walmart-trends/storefront/enrich",
                    json={
                        "post_id": post_id,
                        "include_collages": False,
                        "include_posts": True,
                    },
                    headers={"X-Walmart-Trends-Admin-Token": "test-token"},
                )
        finally:
            os.environ.pop("WALMART_TRENDS_ADMIN_TOKEN", None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["posts_changed"], 1)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["smart_link"], "https://goto.walmart.com/c/3590891/1398372/16662?u=post")
        self.assertEqual(row["product_price"], "$7.77")
        self.assertEqual(row["product_availability"], "In stock")
        self.assertEqual(row["product_rating"], 4.7)
        self.assertEqual(row["product_review_count"], 321)

        html = self.client.get("/shop/posts").get_data(as_text=True)
        self.assertIn("In stock", html)
        self.assertIn("321 reviews", html)

    def test_chat_from_collection_can_return_product_from_another_page(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO collages (slug, products_json, creator_id, status, caption)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "current-page",
                    json.dumps([{
                        "asin": "WMCURRENT",
                        "product_name": "Current Page Bin",
                        "network": "walmart",
                        "retailer": "Walmart",
                        "attribution_link": "https://goto.walmart.com/current",
                    }]),
                    "everydaywithsteph",
                    "published",
                    "Storage",
                ),
            )
            conn.execute(
                """
                INSERT INTO collages (slug, products_json, creator_id, status, caption)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "other-page",
                    json.dumps([{
                        "asin": "WMOTHER",
                        "product_name": "Character Room Sheet Set",
                        "network": "walmart",
                        "retailer": "Walmart",
                        "price_display": "$7.77",
                        "attribution_link": "https://goto.walmart.com/other",
                    }]),
                    "everydaywithsteph",
                    "published",
                    "Character bedding",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        data = self.client.post("/api/shop/chat", json={
            "message": "character sheet set",
            "creator_id": "everydaywithsteph",
            "slug": "current-page",
        }).get_json()
        self.assertEqual(data["products"][0]["asin"], "WMOTHER")


if __name__ == "__main__":
    unittest.main()
