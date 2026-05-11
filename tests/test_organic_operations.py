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

    def test_organic_nav_and_manage_routes_are_available(self):
        html = self.client.get("/archer/organic").get_data(as_text=True)
        self.assertIn("/archer/posts/manage", html)
        self.assertIn("/insights", html)
        self.assertIn("/admin/creators", html)
        self.assertIn("Manage Collections", html)

    def test_post_id_on_organic_redirects_to_dedicated_edit_page(self):
        post_id = self._insert_post(status="draft")
        resp = self.client.get(f"/archer/organic?post_id={post_id}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/archer/posts/{post_id}/edit", resp.headers["Location"])

        edit = self.client.get(f"/archer/posts/{post_id}/edit")
        self.assertEqual(edit.status_code, 200)
        self.assertIn("Edit Organic Post", edit.get_data(as_text=True))

    def test_manage_page_lists_posts_without_using_build_queue(self):
        self._insert_post(status="approved")
        resp = self.client.get("/archer/posts/manage")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Saved Posts & Collections", html)
        self.assertIn("Dinosaur Toys Magnetic Tiles", html)
        self.assertIn("Open Collage Builder", html)


if __name__ == "__main__":
    unittest.main()
