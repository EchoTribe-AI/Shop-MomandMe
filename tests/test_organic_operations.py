import os
import sqlite3
import tempfile
import unittest


class DummyArcher:
    def __init__(self, db_path):
        self.db_path = db_path

    def _db_connect(self):
        return sqlite3.connect(self.db_path)

    def get_by_asins(self, _asins):
        return []

    def get_product(self, _asin):
        return {}


class OrganicOperationsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "organic.db")
        os.environ["CACHE_DB_PATH"] = self.db_path

        import db_schema
        import product_api
        import app

        db_schema.DB_PATH = self.db_path
        product_api.ArcherAPI.CACHE_DB = self.db_path
        db_schema.bootstrap()
        self.app_module = app
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_post(self, status="approved", smart_link="", network="amazon"):
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
                    "B0CQMZFP4H",
                    network,
                    "gift-idea",
                    "This is the post copy.",
                    status,
                    smart_link,
                    "Dinosaur Toys Magnetic Tiles",
                    "Coodoo",
                    "$19.99",
                    "",
                    f"gift-b0cqmzfp4h-{status}",
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def test_product_lookup_uses_full_catalog_fallback_for_known_asin(self):
        import product_lookup_service

        product = product_lookup_service.resolve_amazon_product(
            "B0CQMZFP4H",
            archer=DummyArcher(self.db_path),
            persist=True,
        )
        self.assertEqual(product["asin"], "B0CQMZFP4H")
        self.assertIn("Dinosaur Toys Magnetic Tiles", product["product_name"])
        self.assertEqual(product["company_name"], "Coodoo")
        self.assertEqual(product["price"], "$19.99")
        self.assertIn("ASIN=B0CQMZFP4H", product["image_encoded_string"])

    def test_shop_posts_excludes_drafts_and_renders_shop_amazon_fallback_cta(self):
        self._insert_post(status="draft")
        self._insert_post(status="approved")

        resp = self.client.get("/shop/posts")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Shop Amazon", html)
        self.assertIn("https://www.amazon.com/dp/B0CQMZFP4H?tag=mommymedeals-20", html)
        self.assertEqual(html.count("This is the post copy."), 1)

    def test_post_patch_persists_smart_link_for_public_cta(self):
        post_id = self._insert_post(status="approved")
        smart = "https://go.urlgeni.us/organic-test"
        resp = self.client.patch(f"/archer/posts/{post_id}", json={"smart_link": smart})
        self.assertEqual(resp.status_code, 200)

        html = self.client.get("/shop/posts").get_data(as_text=True)
        self.assertIn("Shop Amazon", html)
        self.assertIn(smart, html)

    # test_organic_nav_and_manage_routes_are_available removed in the
    # Shop-MomandMe strip-down — /archer/organic page deleted along with
    # the organic_posts.html template. The post edit page lives at
    # /archer/posts/<id>/edit and is covered by tests below.

    def test_post_edit_page_renders_for_existing_post(self):
        post_id = self._insert_post(status="draft")
        edit = self.client.get(f"/archer/posts/{post_id}/edit")
        self.assertEqual(edit.status_code, 200)
        html = edit.get_data(as_text=True)
        self.assertIn("Edit Organic Post", html)
        self.assertIn("This is the post copy.", html)
        self.assertNotIn("built-in method copy", html)
        self.assertIn("Create Smart Link", html)

    def test_manage_page_lists_posts_without_using_build_queue(self):
        self._insert_post(status="approved")
        resp = self.client.get("/archer/posts/manage")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Saved Posts & Collections", html)
        self.assertIn("Dinosaur Toys Magnetic Tiles", html)
        # Post-redesign (cherry-pick a052459): the manage page provides a
        # primary "Create" CTA that routes to the Trending Now flow instead
        # of the legacy "Open Collage Builder" link.
        self.assertIn('href="/walmart/trending-now?admin=1"', html)

    def test_posts_schema_has_urlgenius_metadata_columns(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        finally:
            conn.close()
        self.assertIn("smart_link_id", cols)
        self.assertIn("smart_link_affiliate_url", cols)
        self.assertIn("smart_link_final_url", cols)

    def test_smart_link_route_returns_urlgenius_id_and_patch_stores_it(self):
        import product_api

        real_urlgenius = product_api.URLGeniusAPI

        class FakeURLGeniusAPI:
            api_key = "test-key"

            @staticmethod
            def _append_utms(*args, **kwargs):
                return real_urlgenius._append_utms(*args, **kwargs)

            def create_link(self, destination_url, **kwargs):
                final_url = self._append_utms(destination_url, **{
                    k: v for k, v in kwargs.items() if k.startswith("utm_")
                })
                return {
                    "link": {
                        "id": "ug_123",
                        "genius_url": "https://urlgeni.us/amazon/abc",
                        "final_url": final_url,
                    }
                }

        product_api.URLGeniusAPI = FakeURLGeniusAPI
        try:
            resp = self.client.post("/urlgenius/smart_link", json={
                "asin": "B0CQMZFP4H",
                "network": "amazon",
                "placement": {
                    "source": "facebook",
                    "medium": "organic_social",
                    "campaign": "coodoo_dino_organic",
                    "content": "organic_gift_static",
                },
            })
        finally:
            product_api.URLGeniusAPI = real_urlgenius

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["genius_url"], "https://urlgeni.us/amazon/abc")
        self.assertEqual(data["link_id"], "ug_123")
        self.assertIn("tag=mommymedeals-20", data["affiliate_url"])
        self.assertIn("utm_content=organic_gift_static", data["final_url"])

        post_id = self._insert_post(status="approved")
        patch = self.client.patch(f"/archer/posts/{post_id}", json={
            "smart_link": data["genius_url"],
            "smart_link_id": data["link_id"],
            "smart_link_affiliate_url": data["affiliate_url"],
            "smart_link_final_url": data["final_url"],
        })
        self.assertEqual(patch.status_code, 200)
        saved = patch.get_json()["post"]
        self.assertEqual(saved["smart_link_id"], "ug_123")
        self.assertEqual(saved["smart_link"], "https://urlgeni.us/amazon/abc")

    # test_generate_posts_auto_creates_and_stores_urlgenius_link removed
    # in the Shop-MomandMe strip-down — the /archer/generate_posts route
    # was deleted (no KEEP-template caller). Post creation still happens
    # via the /archer/posts CRUD routes which are covered by other tests
    # in this file.


if __name__ == "__main__":
    unittest.main()
