"""Smoke coverage for the three admin surfaces redesigned in
feature/admin-surface-redesigns:

  1. /hub                                — V1-B urgency-first dashboard
  2. /walmart/trending-now?admin=1       — branded admin polish
  3. /collections/<slug>/create-post     — 3-step editor polish

The contract these tests lock in is strictly visual surface presence:
template-level structural elements, brand-vars include, and
retailer-CTA color preservation (per task brief — retailer CTA colors
must stay retailer-specific even after the admin polish).
"""

import os
import tempfile
import unittest
from unittest.mock import patch


class AdminSurfaceRedesignsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "admin-redesigns.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self._admin_env = {
            key: os.environ.pop(key, None)
            for key in (
                "WALMART_TRENDS_ADMIN_TOKEN",
                "ADMIN_API_TOKEN",
                "ADMIN_SECRET",
            )
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
            sess["admin_authed"] = True

    def tearDown(self):
        for key, value in self._admin_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        self.tmp.cleanup()

    # ── 1. /hub — V1-B urgency-first dashboard ──────────────────────────
    def test_hub_renders_v1b_dashboard(self):
        resp = self.client.get("/hub")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)

        # Surface marker so we never silently regress to the old layout.
        self.assertIn(
            'data-surface="hub-v1b"',
            body,
            "/hub did not render the V1-B urgency-first dashboard surface "
            "(missing data-surface=\"hub-v1b\"). Did the old Content Hub "
            "template come back?",
        )

        # Greeting header
        self.assertIn("hub-greet", body)

        # Snapshot tiles
        self.assertIn("Today's snapshot", body)
        self.assertIn("Published", body)
        self.assertIn("Drafts", body)

        # Manage Collections hero card
        self.assertIn("Manage your collections", body)

        # Quick actions — all four CTAs
        self.assertIn("Find trending products", body)
        self.assertIn("Manage collections", body)
        self.assertIn("Chat with EchoAgent", body)
        self.assertIn("Insights", body)

        # Quick-action hrefs are the canonical routes from the brief.
        self.assertIn('href="/walmart/trending-now?admin=1"', body)
        self.assertIn('href="/archer/posts/manage"', body)
        self.assertIn('href="/chat"', body)
        self.assertIn('href="/insights"', body)

        # Recent activity section is present (may be empty on first boot)
        self.assertIn("Recent activity", body)

        # Brand-vars contract — per-creator brand swap must still work.
        self.assertIn('id="brand-vars"', body)

        # active_nav wiring → bottom nav highlights Home.
        self.assertIn('aria-current="page"', body)

    # ── 2. Trends admin — branded polish ─────────────────────────────────
    def test_trends_admin_branded_polish(self):
        resp = self.client.get("/walmart/trending-now?admin=1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)

        # Brand-vars must still include (regression lock).
        self.assertIn('id="brand-vars"', body)

        # New admin-branded banner element
        self.assertIn('class="admin-banner"', body)
        self.assertIn("Trends workspace", body)

        # Workbook Import section is preserved
        self.assertIn("Workbook Import", body)

        # Retailer CTA color rules must remain in the rendered stylesheet
        # so Amazon yellow / Walmart-orange CTAs keep their identity even
        # after the sage/linen admin overlay.
        self.assertIn(".cta-amazon", body, "Amazon CTA color rule was removed")
        self.assertIn(".badge-retailer-amazon", body)
        self.assertIn(".badge-retailer-walmart", body)

    def test_trends_public_has_no_admin_banner(self):
        # Public view (no ?admin=1) must NOT render the admin banner element.
        resp = self.client.get("/trends")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn('<div class="admin-banner"', body)
        self.assertNotIn("Trends workspace", body)

    # ── 3. Collection editor — 3-step polish ────────────────────────────
    def test_collection_editor_three_step_polish(self):
        slug = "admin-redesign-smoke"
        source = {
            "slug": slug,
            "name": "Admin Redesign Smoke",
            "description": "Smoke test collection",
            "items": [
                {
                    "sku": "WM01",
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
        with patch.object(
            self.collection_content, "get_walmart_collection", return_value=source
        ):
            resp = self.client.get(f"/collections/{slug}/create-post")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)

        # 3-step indicator
        self.assertIn("editor-stepper", body)
        self.assertIn("editor-step--active", body)
        self.assertIn(">Content", body)
        self.assertIn(">Design", body)
        self.assertIn(">Preview", body)

        # Brand-vars contract
        self.assertIn('id="brand-vars"', body)

        # Save/publish behavior preserved — primary action still wired
        self.assertIn("Save changes", body)
        self.assertIn("saveChanges()", body)


if __name__ == "__main__":
    unittest.main()
