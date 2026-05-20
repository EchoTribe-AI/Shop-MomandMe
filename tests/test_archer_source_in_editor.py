"""Phase 3 coverage for the source-resolver dispatch + Archer-catalog drafts.

`collection_content.resolve_source_collection` is the single chokepoint for
loading an editor-shaped collection from any supported source. This file
covers:

  1. Dispatch by ``source_type`` (walmart_trend default vs. archer_catalog).
  2. Draft round-trip with ``source_type='archer_catalog'`` — saving a draft
     and re-reading it must scope by source_type so a walmart_trend lookup on
     the same slug does NOT see an archer_catalog draft (isolation).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


class ArcherSourceInEditorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "archer-source.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        # Ensure SQLite fallback (no PG) for these tests.
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)

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
        self.collection_service = collection_service
        self.product_api = product_api
        self.app_module = app
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def tearDown(self):
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url
        self.tmp.cleanup()

    # ── Dispatch ──────────────────────────────────────────────────────────

    def test_resolve_source_collection_dispatches_to_archer_catalog(self):
        cc = self.collection_content
        with patch.object(cc, "load_archer_catalog_collection",
                          return_value={"slug": "x", "items": []}) as archer_loader, \
             patch.object(cc, "get_walmart_collection",
                          return_value=None) as walmart_loader:
            result = cc.resolve_source_collection("nursery", "archer_catalog")
        archer_loader.assert_called_once_with("nursery")
        walmart_loader.assert_not_called()
        self.assertEqual(result["slug"], "x")

    def test_resolve_source_collection_defaults_to_walmart_trend(self):
        cc = self.collection_content
        sentinel = {"slug": "kids-room", "items": [{"sku": "WM001"}]}
        with patch.object(cc, "load_archer_catalog_collection",
                          return_value=None) as archer_loader, \
             patch.object(cc, "get_walmart_collection",
                          return_value=sentinel) as walmart_loader:
            result = cc.resolve_source_collection("kids-room", "walmart_trend")
        walmart_loader.assert_called_once_with("kids-room")
        archer_loader.assert_not_called()
        self.assertEqual(result, sentinel)

    def test_resolve_source_collection_unknown_type_falls_back_to_walmart(self):
        cc = self.collection_content
        with patch.object(cc, "load_archer_catalog_collection",
                          return_value=None) as archer_loader, \
             patch.object(cc, "get_walmart_collection",
                          return_value={"slug": "x"}) as walmart_loader:
            cc.resolve_source_collection("anything", "totally_unknown_source")
        # Anything that's not SOURCE_ARCHER_CATALOG goes through the walmart path.
        walmart_loader.assert_called_once()
        archer_loader.assert_not_called()

    # ── Archer catalog loader uses ArcherAPI.search_catalog ───────────────

    def test_load_archer_catalog_collection_uses_archer_search(self):
        cc = self.collection_content
        fake_rows = [
            {
                "asin": "B0AA000001",
                "product_name": "Fake Catalog Find",
                "company_name": "BrandX",
                "price": "$9.99",
                "image_encoded_string": "https://i.example/a.jpg",
                "product_category": "Nursery",
            },
        ]
        with patch.object(self.product_api.ArcherAPI, "search_catalog",
                          return_value=fake_rows) as searcher:
            coll = cc.load_archer_catalog_collection("nursery-finds")
        searcher.assert_called_once()
        self.assertIsNotNone(coll)
        self.assertEqual(coll["slug"], "nursery-finds")
        self.assertEqual(len(coll["items"]), 1)
        item = coll["items"][0]
        self.assertEqual(item["sku"], "B0AA000001")
        self.assertEqual(item["retailer"], "amazon")
        self.assertEqual(item["network"], "amazon")
        self.assertIn("amazon.com/dp/B0AA000001", item["shop_url"])

    # ── Draft round-trip (archer_catalog source) ──────────────────────────

    def test_archer_catalog_draft_round_trip_with_source_isolation(self):
        cc = self.collection_content
        fake_rows = [
            {
                "asin": "B0DRAFTRT1",
                "product_name": "Archer Draft Find",
                "company_name": "BrandY",
                "price": "$12.34",
                "image_encoded_string": "https://i.example/b.jpg",
                "product_category": "Bath",
            },
        ]
        slug = "bath-time-finds"
        payload = {
            "title": "Bath time finds",
            "description": "Curated picks",
            "social_post": "Bath night!",
            "landing_intro": "These are great",
            "voice_source_text": "Bath finds rock",
            "hooks": [
                {"type": "Fast Discovery", "text": "Tub-time wins"},
                {"type": "Problem", "text": "Slippery bath"},
                {"type": "Faves", "text": "Mom-tested"},
            ],
            "cta": "Shop these finds",
            "platform": "facebook_group",
            "tone": "warm",
            "creator_id": "everydaywithsteph",
            "theme": "mommyme",
            "layout": "layout-2",
        }
        with patch.object(self.product_api.ArcherAPI, "search_catalog",
                          return_value=fake_rows):
            saved = cc.save_walmart_collection_draft(
                slug,
                payload,
                status="draft",
                source_type=cc.SOURCE_ARCHER_CATALOG,
            )
        self.assertIsNotNone(saved)
        self.assertEqual(saved.get("source_type"), cc.SOURCE_ARCHER_CATALOG)
        self.assertEqual(saved.get("source_collection_slug"), slug)

        # Re-read scoped to archer_catalog: must find the saved draft.
        archer_draft = cc.get_latest_draft_for_source_collection(
            slug,
            "everydaywithsteph",
            source_type=cc.SOURCE_ARCHER_CATALOG,
        )
        self.assertIsNotNone(archer_draft)
        self.assertEqual(archer_draft["source_type"], cc.SOURCE_ARCHER_CATALOG)
        self.assertEqual(archer_draft["source_collection_slug"], slug)

        # Re-read scoped to walmart_trend on the SAME slug: must NOT find it.
        walmart_draft = cc.get_latest_draft_for_source_collection(
            slug,
            "everydaywithsteph",
            source_type=cc.SOURCE_WALMART_TREND,
        )
        self.assertIsNone(walmart_draft,
            "walmart_trend lookup must not return an archer_catalog draft "
            "(source-scoped isolation is the whole point of source_type)")


if __name__ == "__main__":
    unittest.main()
