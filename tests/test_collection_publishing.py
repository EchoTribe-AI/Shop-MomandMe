import importlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch


class CollectionPublishingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "collections.db")
        os.environ["CACHE_DB_PATH"] = self.db_path

        import db_schema
        import collection_service
        import product_api
        import collection_content
        import app

        db_schema.DB_PATH = self.db_path
        collection_service.db_schema.DB_PATH = self.db_path
        collection_content.db_schema.DB_PATH = self.db_path
        product_api.ArcherAPI.CACHE_DB = self.db_path

        db_schema.bootstrap()
        self.db_schema = db_schema
        self.collection_service = importlib.reload(collection_service)
        self.product_api = product_api
        self.app_module = app
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def tearDown(self):
        self.tmp.cleanup()

    def _columns(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    def _save(self, slug, status="draft", products=None):
        return self.client.post("/archer/collage/save", json={
            "slug": slug,
            "status": status,
            "products": products or [{"asin": "B000000001", "product_name": "Test Product"}],
            "caption": "Original caption",
            "theme": "coral",
        })

    def test_fresh_boot_creates_collages_with_required_columns(self):
        columns = set(self._columns("collages"))
        self.assertTrue({
            "slug", "products_json", "layout", "theme", "caption",
            "direct_to_amazon", "created_at", "click_count", "creator_id",
            "status", "campaign_types", "hero_title", "hero_subtitle",
        }.issubset(columns))

    @unittest.skip(
        "Obsolete after PG migration: ArcherAPI._db_connect() now delegates to "
        "db_schema._connect(), so ArcherAPI.CACHE_DB is no longer consulted. The "
        "cross-DB-pollution concern this test guarded is resolved at the "
        "architecture level — all tables now live in a single backing store "
        "(PostgreSQL when DATABASE_URL is set, otherwise CACHE_DB_PATH SQLite). "
        "See Replit migration commit fe27467."
    )
    def test_archer_init_cache_does_not_create_collages(self):
        other_db = os.path.join(self.tmp.name, "archer-only.db")
        with patch.object(self.product_api.ArcherAPI, "CACHE_DB", other_db):
            archer = self.product_api.ArcherAPI.__new__(self.product_api.ArcherAPI)
            archer._init_cache()
        conn = sqlite3.connect(other_db)
        try:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='collages'"
            ).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='products'"
            ).fetchone())
        finally:
            conn.close()

    def test_draft_save_returns_preview_and_skips_archer_link_generation(self):
        with patch("product_api.ArcherAPI.generate_link") as generate:
            resp = self._save("draft-page", status="draft")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["public_url"], "/shop/draft-page?preview=1")
        generate.assert_not_called()

    def test_collage_builder_exposes_draft_publish_and_management_actions(self):
        resp = self.client.get("/archer/collage")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Save Draft", html)
        self.assertIn("Publish", html)
        self.assertIn("Manage Collections", html)
        self.assertIn("saveDraftCollage()", html)
        self.assertIn("publishCurrentCollage()", html)
        self.assertIn("publishSavedCollage", html)

    def test_publish_flips_status_and_returns_public_url(self):
        self._save("publish-me", status="draft")
        with patch("product_api.ArcherAPI.generate_link", return_value={"url": "https://archer.example/publish-me"}):
            resp = self.client.post("/archer/collage/publish", json={"slug": "publish-me"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "published")
        self.assertEqual(data["public_url"], "https://shop.echotribe.ai/publish-me")

    def test_resave_preserves_click_count_created_at_and_campaign_types(self):
        with patch("product_api.ArcherAPI.generate_link", return_value={"url": "https://archer.example/keep-metadata"}):
            resp = self._save("keep-metadata", status="published")
        self.assertEqual(resp.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE collages SET click_count = 12, campaign_types = ? WHERE slug = ?",
                ('["organic","paid"]', "keep-metadata"),
            )
            before = conn.execute(
                "SELECT created_at FROM collages WHERE slug = ?", ("keep-metadata",)
            ).fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        with patch("product_api.ArcherAPI.generate_link", return_value={"url": "https://archer.example/keep-metadata-2"}):
            resp = self._save("keep-metadata", status="published")
        self.assertEqual(resp.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT click_count, created_at, campaign_types FROM collages WHERE slug = ?",
                ("keep-metadata",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 12)
        self.assertEqual(row[1], before)
        self.assertIn("paid", row[2])

    def test_walmart_links_are_preserved_and_skip_archer_generation(self):
        walmart_url = "https://goto.walmart.com/c/3590891/1398372/16662?u=exact"
        product = {
            "asin": "5454929532",
            "product_name": "Walmart Find",
            "network": "walmart",
            "retailer": "Walmart",
            "attribution_link": walmart_url,
        }
        with patch("product_api.ArcherAPI.generate_link") as generate:
            resp = self._save("walmart-page", status="published", products=[product])
        self.assertEqual(resp.status_code, 200)
        generate.assert_not_called()
        saved = self.collection_service.get_collage("walmart-page")
        self.assertEqual(saved["products"][0]["attribution_link"], walmart_url)

    def test_walmart_missing_attribution_link_is_rejected_on_publish(self):
        product = {
            "asin": "5454929532",
            "product_name": "Walmart Find",
            "network": "walmart",
            "retailer": "Walmart",
        }
        resp = self._save("bad-walmart-page", status="published", products=[product])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("missing attribution_link", resp.get_json()["error"])

    def test_collage_list_status_filter(self):
        self._save("draft-row", status="draft")
        with patch("product_api.ArcherAPI.generate_link", return_value={"url": "https://archer.example/published-row"}):
            self._save("published-row", status="published")

        published = self.client.get("/archer/collages?status=published").get_json()["collages"]
        self.assertEqual([row["slug"] for row in published], ["published-row"])

        all_rows = self.client.get("/archer/collages?status=all").get_json()["collages"]
        self.assertEqual({row["slug"] for row in all_rows}, {"draft-row", "published-row"})

    def test_amazon_only_draft_publishes_with_amazon_cta(self):
        amazon_product = {
            "asin": "B0CXAMZN01",
            "product_name": "Amazon Find",
            "network": "amazon",
            "retailer": "Amazon",
            "attribution_link": "https://www.amazon.com/dp/B0CXAMZN01?tag=mommymedeals-20",
            "image_encoded_string": "https://i.example/amzn.jpg",
        }
        with patch("product_api.ArcherAPI.generate_link") as generate:
            resp = self._save("amazon-only", status="published", products=[amazon_product])
        self.assertEqual(resp.status_code, 200)
        generate.assert_not_called()
        page = self.client.get("/shop/amazon-only")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Shop Amazon →", html)
        self.assertNotIn("Shop Walmart →", html)

    def test_walmart_only_draft_publishes_with_walmart_cta(self):
        walmart_product = {
            "asin": "5454929532",
            "product_name": "Walmart Find",
            "network": "walmart",
            "retailer": "Walmart",
            "attribution_link": "https://goto.walmart.com/c/3590891/1398372/16662?u=wm",
            "image_encoded_string": "https://i.example/wm.jpg",
        }
        with patch("product_api.ArcherAPI.generate_link") as generate:
            resp = self._save("walmart-only", status="published", products=[walmart_product])
        self.assertEqual(resp.status_code, 200)
        generate.assert_not_called()
        page = self.client.get("/shop/walmart-only")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Shop Walmart →", html)
        self.assertNotIn("Shop Amazon →", html)

    def test_shop_visibility_for_draft_and_published(self):
        self._save("visibility-draft", status="draft")
        self.assertEqual(self.client.get("/shop/visibility-draft").status_code, 404)
        self.assertEqual(self.client.get("/shop/visibility-draft?preview=1").status_code, 200)

        with patch("product_api.ArcherAPI.generate_link", return_value={"url": "https://archer.example/visibility"}):
            self.client.post("/archer/collage/publish", json={"slug": "visibility-draft"})
        self.assertEqual(self.client.get("/shop/visibility-draft").status_code, 200)


if __name__ == "__main__":
    unittest.main()
