"""Render-smoke coverage for the 4 admin surfaces touched by the
brand-polish sweep (feature/brand-polish-admin-surfaces).

Each surface must include partials/_brand_vars.html so the canonical
brand-swap CSS vars (--brand-primary, --brand-on-primary, --brand-surface,
…) resolve from the active creator row. The include emits
`<style id="brand-vars">`, so its presence in rendered HTML is the
single contract this test locks in.

Why this exists: admin_login, hub, and the collection editor previously
rendered with only the static Creator Core palette. Forgetting the
include silently breaks per-creator brand swap on these screens without
any test failure. This file is the regression net.
"""

import os
import tempfile
import unittest
from unittest.mock import patch


class BrandPolishRendersTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "brand-polish.db")
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
        self.collection_content = collection_content
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def tearDown(self):
        for key, value in self._admin_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        self.tmp.cleanup()

    # ---- The contract: every brand-polish surface emits <style id="brand-vars">.

    def _assert_brand_vars_emitted(self, html: str, surface: str) -> None:
        self.assertIn(
            'id="brand-vars"',
            html,
            f"{surface} is missing the _brand_vars.html include — "
            "per-creator brand swap will silently fall back to Creator Core.",
        )

    def test_admin_login_includes_brand_vars(self):
        # Anonymous GET — login form renders pre-auth.
        # Use a fresh client so the session_transaction admin seed doesn't
        # short-circuit into a redirect.
        import app
        client = app.app.test_client()
        resp = client.get('/admin/login')
        self.assertEqual(resp.status_code, 200)
        self._assert_brand_vars_emitted(resp.get_data(as_text=True), "admin_login")

    def test_hub_includes_brand_vars(self):
        resp = self.client.get('/hub')
        self.assertEqual(resp.status_code, 200)
        self._assert_brand_vars_emitted(resp.get_data(as_text=True), "hub")

    def test_trending_now_admin_includes_brand_vars(self):
        resp = self.client.get('/walmart/trending-now?admin=1')
        self.assertEqual(resp.status_code, 200)
        self._assert_brand_vars_emitted(
            resp.get_data(as_text=True), "walmart_trending_now (admin)"
        )

    def test_collection_editor_includes_brand_vars(self):
        slug = "brand-polish-smoke"
        source = {
            "slug": slug,
            "name": "Brand Polish Smoke",
            "description": "Smoke test collection",
            "items": [
                {
                    "sku": "WM001",
                    "title": "Smoke Item",
                    "brand": "WalmartBrand",
                    "price_display": "$1.00",
                    "image_url": "https://i.example/1.jpg",
                    "shop_url": "https://goto.walmart.com/c/test",
                    "category": "Smoke",
                    "rank": 1,
                }
            ],
        }
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            resp = self.client.get(f'/collections/{slug}/create-post')
        self.assertEqual(resp.status_code, 200)
        self._assert_brand_vars_emitted(
            resp.get_data(as_text=True), "walmart_collection_create_post"
        )


if __name__ == "__main__":
    unittest.main()
