"""Phase 3 coverage for the legacy `/archer/*` → new path 307 redirects.

Phase 1A restructured the URL surface to neutral admin/API namespaces. To
keep external links (bookmarks, scheduled posts, browser history) working
for the 30-day deprecation window, every old `/archer/*` path emits a
307 (Temporary Redirect, method-preserving) to its new home.

This test pins the mapping from the plan §2 and verifies:
  - The status code is exactly 307 (so POST/PATCH/DELETE bodies are preserved).
  - The Location header matches the expected new path.
  - Query strings are preserved on the redirects that propagate them.
"""
from __future__ import annotations

import os
import tempfile
import unittest


class LegacyArcherRedirectsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "redirects.db")
        os.environ["CACHE_DB_PATH"] = cls.db_path
        cls._saved_db_url = os.environ.pop("DATABASE_URL", None)

        import db_schema
        import collection_service
        import collection_content
        import product_api
        import app

        db_schema.DB_PATH = cls.db_path
        collection_service.db_schema.DB_PATH = cls.db_path
        collection_content.db_schema.DB_PATH = cls.db_path
        product_api.ArcherAPI.CACHE_DB = cls.db_path
        db_schema.bootstrap()
        cls.app_module = app
        cls.client = app.app.test_client()
        # Authenticate so the guards on the legacy stubs (added back
        # alongside the new neutral routes) don't 302→/admin/login
        # before the redirect can fire. Page-guarded stubs need a session;
        # API-guarded stubs accept the same session.
        with cls.client.session_transaction() as sess:
            sess['admin_authed'] = True

    @classmethod
    def tearDownClass(cls):
        if cls._saved_db_url is not None:
            os.environ["DATABASE_URL"] = cls._saved_db_url
        cls.tmp.cleanup()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _assert_redirect(self, method: str, src: str, expected_location: str,
                         json_body: dict | None = None):
        kwargs = {"follow_redirects": False}
        if json_body is not None:
            kwargs["json"] = json_body
        resp = getattr(self.client, method.lower())(src, **kwargs)
        self.assertEqual(
            resp.status_code, 307,
            f"{method} {src!r} expected 307; got {resp.status_code}. "
            "307 is required so request bodies (POST/PATCH) survive the hop.",
        )
        loc = resp.headers.get("Location", "")
        # Flask normalizes to absolute in some envs; strip host if present.
        if loc.startswith("http://") or loc.startswith("https://"):
            from urllib.parse import urlparse
            parsed = urlparse(loc)
            loc = parsed.path + (("?" + parsed.query) if parsed.query else "")
        self.assertEqual(
            loc, expected_location,
            f"{method} {src!r} redirected to {loc!r}; expected {expected_location!r}",
        )

    # ── Click tracking ────────────────────────────────────────────────────

    def test_track_click_redirect(self):
        self._assert_redirect("POST", "/archer/track_click", "/api/clicks",
                              json_body={"asin": "B0X", "slug": "y"})

    # ── Product search + lookup ───────────────────────────────────────────

    def test_search_preserves_query_string(self):
        self._assert_redirect("GET", "/archer/search?q=foo",
                              "/api/products/search?q=foo")

    def test_product_lookup_redirect(self):
        self._assert_redirect("GET", "/archer/product/B0ABC",
                              "/api/products/B0ABC")

    # ── Collection editor + storage ───────────────────────────────────────

    def test_collage_editor_redirect_bare(self):
        self._assert_redirect("GET", "/archer/collage",
                              "/admin/collections/edit")

    def test_collage_editor_redirect_with_collection(self):
        self._assert_redirect("GET", "/archer/collage?collection=foo",
                              "/admin/collections/edit?collection=foo")

    def test_collage_get_by_slug_redirect(self):
        self._assert_redirect("GET", "/archer/collage/some-slug",
                              "/api/collections/some-slug")

    def test_collage_save_redirect(self):
        self._assert_redirect("POST", "/archer/collage/save",
                              "/api/collections/draft",
                              json_body={"slug": "x", "products": []})

    def test_collage_publish_redirect(self):
        self._assert_redirect("POST", "/archer/collage/publish",
                              "/api/collections/publish",
                              json_body={"slug": "x"})

    def test_collage_archive_redirect(self):
        self._assert_redirect("POST", "/archer/collage/archive",
                              "/api/collections/archive",
                              json_body={"slug": "x"})

    def test_collage_restore_redirect(self):
        self._assert_redirect("POST", "/archer/collage/restore",
                              "/api/collections/restore",
                              json_body={"slug": "x"})

    def test_collage_list_redirect(self):
        self._assert_redirect("GET", "/archer/collages",
                              "/api/collections")

    # ── Posts queue ───────────────────────────────────────────────────────

    def test_posts_manage_page_redirect(self):
        self._assert_redirect("GET", "/archer/posts/manage",
                              "/admin/posts")

    def test_post_edit_page_redirect(self):
        self._assert_redirect("GET", "/archer/posts/12/edit",
                              "/admin/posts/12/edit")

    def test_posts_list_redirect(self):
        self._assert_redirect("GET", "/archer/posts",
                              "/api/posts")

    def test_post_patch_redirect(self):
        self._assert_redirect("PATCH", "/archer/posts/12",
                              "/api/posts/12",
                              json_body={"status": "approved"})

    def test_post_delete_redirect(self):
        self._assert_redirect("DELETE", "/archer/posts/12",
                              "/api/posts/12")

    def test_posts_bulk_redirect(self):
        self._assert_redirect("POST", "/archer/posts/bulk",
                              "/api/posts/bulk",
                              json_body={"ids": [1], "status": "approved"})

    def test_posts_export_csv_redirect(self):
        self._assert_redirect("GET", "/archer/posts/export.csv",
                              "/api/posts/export.csv")

    # ── Misc ──────────────────────────────────────────────────────────────

    def test_generate_caption_redirect(self):
        self._assert_redirect("POST", "/archer/generate_caption",
                              "/api/captions/generate",
                              json_body={"asin": "B0X"})

    def test_image_proxy_redirect(self):
        self._assert_redirect(
            "GET",
            "/archer/image_proxy?url=https%3A%2F%2Fi.example%2Fa.jpg",
            "/api/image_proxy?url=https%3A%2F%2Fi.example%2Fa.jpg",
        )


if __name__ == "__main__":
    unittest.main()
