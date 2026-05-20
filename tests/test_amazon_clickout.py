"""Phase 3 coverage for the USE_ARCHER_ATTRIBUTION feature flag.

The plan §3 contract: with the flag OFF (the default), no Amazon click-out
path should call ``ArcherAPI.generate_link``. We exercise the three call
sites that previously did so:

  1. ``link_builder.ArcherURLGenius.build`` — used by the smart-link helper
     and the save/publish closures in app.py.
  2. The collection_service save/publish closures in app.py (driven through
     ``/api/collections/draft`` and ``/api/collections/publish``).
  3. ``product_api.py`` Step 2 — the archer-catalog branch of the chat
     product resolver.

Each path must return a URL that contains ``amazon.com/dp/<ASIN>`` and a
``tag=`` query parameter (the plain Amazon affiliate URL we synthesize when
the flag is off).
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class _BaseFlagOffCase(unittest.TestCase):
    """Force USE_ARCHER_ATTRIBUTION off and reload link_builder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "amazon-clickout.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)
        # Default-off semantics: explicit unset to avoid leakage from CI env.
        self._saved_use_archer = os.environ.pop("USE_ARCHER_ATTRIBUTION", None)
        # Lock the affiliate tag so the assertion is deterministic.
        os.environ["AMAZON_AFFILIATE_TAG"] = "mommymedeals-20"

        import db_schema
        import collection_service
        import collection_content
        import product_api
        import link_builder
        import app

        db_schema.DB_PATH = self.db_path
        collection_service.db_schema.DB_PATH = self.db_path
        collection_content.db_schema.DB_PATH = self.db_path
        product_api.ArcherAPI.CACHE_DB = self.db_path
        db_schema.bootstrap()

        # Reload link_builder so its module-level USE_ARCHER_ATTRIBUTION
        # constant picks up the cleared env var. (It only reads os.environ
        # at import time.)
        self.link_builder = importlib.reload(link_builder)
        self.product_api = product_api
        self.app_module = app
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def tearDown(self):
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url
        if self._saved_use_archer is not None:
            os.environ["USE_ARCHER_ATTRIBUTION"] = self._saved_use_archer
        self.tmp.cleanup()


class FlagOffAmazonClickoutTest(_BaseFlagOffCase):
    def test_flag_is_off_by_default(self):
        self.assertFalse(self.link_builder.USE_ARCHER_ATTRIBUTION,
            "USE_ARCHER_ATTRIBUTION must default to False — flipping it on "
            "would re-enable archer outbound calls on every click-out.")

    def test_link_builder_build_skips_archer_generate_link(self):
        builder = self.link_builder.ArcherURLGenius()
        with patch.object(self.product_api.ArcherAPI, "generate_link") as generate:
            result = builder.build("B0TEST123", utm={}, creator={"id": "creator"})
        generate.assert_not_called()
        url = result.get("affiliate_url") or result.get("genius_url") or ""
        self.assertIn("amazon.com/dp/B0TEST123", url)
        self.assertIn("tag=", url)

    def test_build_smart_link_full_path_skips_archer_generate_link(self):
        """The top-level helper used by app.py save/publish closures."""
        with patch.object(self.product_api.ArcherAPI, "generate_link") as generate:
            result = self.link_builder.build_smart_link(
                "B0BUILDSMR",
                network="amazon",
                utm={"source": "fb-group", "medium": "organic",
                     "campaign": "test", "content": "amazon-assoc",
                     "term": "b0buildsmr"},
                creator_id="everydaywithsteph",
            )
        generate.assert_not_called()
        url = result.get("affiliate_url") or result.get("genius_url") or ""
        self.assertIn("amazon.com/dp/B0BUILDSMR", url)
        self.assertIn("tag=", url)

    def test_collection_draft_save_skips_archer_generate_link(self):
        """End-to-end: posting a draft through the API never calls Archer."""
        product = {
            "asin": "B0DRAFTAMZ",
            "product_name": "Draft Amazon Find",
            "network": "amazon",
            "retailer": "Amazon",
            "attribution_link": "",
        }
        with patch.object(self.product_api.ArcherAPI, "generate_link") as generate:
            resp = self.client.post("/api/collections/draft", json={
                "slug": "amazon-flag-off",
                "status": "draft",
                "products": [product],
                "caption": "Flag-off draft",
                "theme": "coral",
            })
        self.assertEqual(resp.status_code, 200)
        generate.assert_not_called()

    def test_product_api_step2_archer_branch_skips_generate_link(self):
        """Chat-side product resolver (product_api.py Step 2) must respect the flag.

        We force the Step 1 (hot-catalog) match list to be empty so Step 2
        runs, and mock ArcherAPI.search_catalog to return a synthetic ASIN.
        With the flag off, generate_link must NOT be called and the formatted
        product link must contain ``amazon.com/dp/<ASIN>?tag=…``.
        """
        product_api = self.product_api
        fake_catalog_row = {
            "asin": "B0STEP2TST",
            "product_name": "Step2 Catalog Product",
            "price": "$1.23",
            "company_name": "BrandZ",
            "image_encoded_string": "https://i.example/step2.jpg",
            "product_category": "Bath",
        }
        # ProductResolver.resolve is the orchestrator that runs the three-step
        # pipeline; we patch its dependencies to isolate Step 2.
        resolver = product_api.ProductResolver(hot_catalog=[])
        with patch.object(resolver, "_search_hot_catalog", return_value=[]), \
             patch.object(resolver.archer_api, "search_catalog",
                          return_value=[fake_catalog_row]), \
             patch.object(resolver.archer_api, "generate_link") as generate, \
             patch.object(resolver.walmart_api, "search", return_value=[]):
            results = resolver.resolve(
                "step2 tester", category=None, max_results=3
            )
        generate.assert_not_called()
        # The formatted result must carry an Amazon affiliate URL with tag=.
        urls = [str(r.get("link") or r.get("url") or "") for r in results]
        joined = " ".join(urls)
        self.assertIn("amazon.com/dp/B0STEP2TST", joined)
        self.assertIn("tag=", joined)


class FlagOnArcherCallTest(_BaseFlagOffCase):
    """Sanity check that the gating actually flips: with USE_ARCHER_ATTRIBUTION on,
    the link_builder path DOES try to call ArcherAPI.generate_link. (We don't
    care about the return value here — only that the gate is wired correctly.)
    """

    def setUp(self):
        super().setUp()
        os.environ["USE_ARCHER_ATTRIBUTION"] = "1"
        import link_builder
        self.link_builder = importlib.reload(link_builder)

    def test_flag_on_invokes_archer_generate_link(self):
        self.assertTrue(self.link_builder.USE_ARCHER_ATTRIBUTION)
        builder = self.link_builder.ArcherURLGenius()
        with patch.object(self.product_api.ArcherAPI, "generate_link",
                          return_value={"url": "https://www.amazon.com/dp/B0FLAGON123?tag=mommymedeals-20"}) as generate:
            builder.build("B0FLAGON123", utm={}, creator={"id": "creator"})
        generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
