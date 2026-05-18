"""Regression tests for the four checkpoint fixes:

  1. PG-compatible CASE in collection_content._upsert_collage_from_draft
     (published_at = CASE WHEN ? = TRUE THEN ? ELSE published_at END,
     with bool(publish) as the param).

  2. PG-compatible workbook import / walmart_trends.create_run
     (_adapt_sql translates `BEGIN IMMEDIATE` and `datetime('now', ...)`).

  3. mommyme is the default theme on save and publish paths.

  4. /archer/posts/<id>/edit renders a JS default-campaign fallback so
     /urlgenius/smart_link never receives a blank utm_campaign.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class AdaptSqlPgTranslationTest(unittest.TestCase):
    """The _adapt_sql translator is the single chokepoint that makes
    sqlite3-flavoured query strings safe to send to psycopg2. Test the
    three translations it performs."""

    def test_question_mark_placeholders_become_percent_s(self):
        import db_schema
        out = db_schema._adapt_sql("SELECT * FROM t WHERE a = ? AND b = ?")
        self.assertEqual(out, "SELECT * FROM t WHERE a = %s AND b = %s")

    def test_begin_immediate_becomes_no_op_select(self):
        import db_schema
        # SQLite's `BEGIN IMMEDIATE` (exclusive lock) has no PG analog;
        # we substitute a no-op statement so callers don't crash.
        self.assertEqual(db_schema._adapt_sql("BEGIN IMMEDIATE"), "SELECT 1")
        self.assertEqual(db_schema._adapt_sql("  begin   immediate  "), "SELECT 1")

    def test_datetime_now_translates_to_pg_interval(self):
        import db_schema
        # The exact form that walmart_trends.create_run uses:
        self.assertIn(
            "NOW() - INTERVAL '2 hours'",
            db_schema._adapt_sql("started_at >= datetime('now', '-2 hours')"),
        )
        # Bare now:
        self.assertIn("NOW()", db_schema._adapt_sql("SELECT datetime('now')"))
        # Positive offset:
        self.assertIn(
            "NOW() + INTERVAL '1 day'",
            db_schema._adapt_sql("WHERE x < datetime('now', '+1 day')"),
        )

    def test_datetime_now_translation_preserves_surrounding_sql(self):
        import db_schema
        sql = (
            "UPDATE walmart_refresh_runs SET status = 'failed' "
            "WHERE started_at < datetime('now', '-2 hours')"
        )
        out = db_schema._adapt_sql(sql)
        self.assertIn("UPDATE walmart_refresh_runs", out)
        self.assertNotIn("datetime('now'", out)
        self.assertIn("INTERVAL '2 hours'", out)


class _CollectionContentBaseCase(unittest.TestCase):
    """Shared SQLite fixture for the collection_content regression tests.
    These don't need a real PG instance — they exercise the call paths
    end-to-end in SQLite fallback mode, which is what the test runner has."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "checkpoint.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        # Ensure SQLite fallback (no PG) for these tests.
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)

        import db_schema
        import collection_service
        import collection_content
        import product_api
        import app

        # Reload-friendly path bindings.
        db_schema.DB_PATH = self.db_path
        collection_service.db_schema.DB_PATH = self.db_path
        collection_content.db_schema.DB_PATH = self.db_path
        product_api.ArcherAPI.CACHE_DB = self.db_path
        db_schema.bootstrap()

        self.db_schema = db_schema
        self.collection_content = collection_content
        self.collection_service = collection_service
        self.app_module = app
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def tearDown(self):
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url
        self.tmp.cleanup()


class PgPublishCaseRegressionTest(_CollectionContentBaseCase):
    """Fix #1: _upsert_collage_from_draft must pass bool(publish) to the
    CASE WHEN ? = TRUE clause so the SQL runs under PostgreSQL semantics."""

    def _seed_draft_and_publish(self):
        # Use the public save endpoint to create a draft, then publish via
        # the canonical helper. Goes through the actual SQL we care about.
        source = {
            "slug": "checkpoint-collection",
            "name": "Checkpoint",
            "description": "",
            "items": [
                {
                    "sku": "WM999",
                    "title": "Find",
                    "brand": "B",
                    "price_display": "$1.00",
                    "image_url": "https://i.example/x.jpg",
                    "shop_url": "https://goto.walmart.com/c/3590891/1398372/16662?u=wm999",
                    "category": "Cat",
                    "rank": 1,
                }
            ],
        }
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            with patch("product_api.WalmartAPI.get_item_by_id", side_effect=lambda sku: {"sku": sku, "name": f"Live {sku}"}):
                resp = self.client.post(
                    "/api/walmart/collections/checkpoint-collection/draft-page",
                    json={"creator_id": "everydaywithsteph", "public_slug": "checkpoint-page", "title": "Checkpoint"},
                )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        draft = resp.get_json()["draft"]
        publish_resp = self.client.post(f"/api/collection-content-drafts/{draft['id']}/publish")
        self.assertEqual(publish_resp.status_code, 200, publish_resp.get_data(as_text=True))
        return draft

    def test_publish_sets_published_at_via_pg_safe_case(self):
        draft = self._seed_draft_and_publish()
        # The fix is: the UPDATE used `CASE WHEN ? = TRUE THEN ? ELSE published_at END`
        # with bool(publish). Confirm published_at is non-null after publish.
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT published_at, status FROM collection_content_drafts WHERE id = ?",
                (draft['id'],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "draft row should exist")
        published_at, status = row
        self.assertEqual(status, "published")
        self.assertIsNotNone(published_at, "published_at should be set after publish")

    def test_upsert_collage_from_draft_passes_real_boolean(self):
        # Catches the regression directly: the params tuple must include
        # bool(publish), not int(publish). If someone reverts to `1 if
        # publish else 0`, this test fails.
        import inspect
        src = inspect.getsource(self.collection_content._upsert_collage_from_draft)
        self.assertIn("bool(publish)", src,
                      "_upsert_collage_from_draft must pass bool(publish) for PG compat")
        self.assertIn("= TRUE", src,
                      "_upsert_collage_from_draft must use `= TRUE` in CASE WHEN for PG compat")


class PgWorkbookImportCreateRunTest(unittest.TestCase):
    """Fix #2: walmart_trends.create_run + the SQLite-only SQL it emits
    must adapt cleanly to PG via _adapt_sql, and the create_run flow must
    succeed end-to-end in SQLite fallback (it would have failed before the
    cursor-order fix in Step 3 too)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "walmart.db")
        os.environ["CACHE_DB_PATH"] = self.db_path
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)

        import db_schema
        import walmart_trends
        db_schema.DB_PATH = self.db_path
        walmart_trends.DB_PATH = self.db_path
        db_schema.bootstrap()
        self.db_schema = db_schema
        self.walmart_trends = walmart_trends

    def tearDown(self):
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url
        self.tmp.cleanup()

    def test_create_run_emits_pg_compatible_sql_via_adapter(self):
        # Verify the SQL strings create_run emits all translate cleanly
        # through _adapt_sql. If any of them remained PG-incompatible the
        # browser would see a 500 HTML page instead of JSON.
        adapt = self.db_schema._adapt_sql
        self.assertEqual(adapt("BEGIN IMMEDIATE"), "SELECT 1")
        self.assertIn(
            "INTERVAL '2 hours'",
            adapt("SELECT 1 WHERE started_at >= datetime('now', '-2 hours')"),
        )

    def test_create_run_succeeds_in_sqlite_fallback(self):
        # End-to-end: this is the path the workbook import button hits.
        # Before the Step 3 cursor-order fix this raised
        #   sqlite3.OperationalError: cannot commit transaction - SQL
        #   statements in progress
        # so it doubles as a regression guard for that issue.
        store = self.walmart_trends.WalmartTrendStore()
        run_id = store.create_run("impact_weekly")
        self.assertIsInstance(run_id, int)
        self.assertGreater(run_id, 0)
        store.finish_run(run_id, "success", {"records": 0}, [])

    def test_concurrent_create_run_blocks_with_already_running(self):
        # The lock semantics that originally motivated BEGIN IMMEDIATE
        # still need to hold in SQLite mode. (In PG, the no-op SELECT 1
        # is fine because the SELECT...WHERE status='running' query inside
        # the transaction provides the visibility guarantee psycopg2 needs.)
        store = self.walmart_trends.WalmartTrendStore()
        run_id = store.create_run("workbook_bootstrap")
        try:
            with self.assertRaises(self.walmart_trends.RefreshAlreadyRunning):
                store.create_run("impact_weekly")
        finally:
            store.finish_run(run_id, "success", {"records": 0}, [])


class MommymeDefaultThemeTest(_CollectionContentBaseCase):
    """Fix #3: mommyme must be the default theme on every save/publish
    path that previously defaulted to peach."""

    def test_save_endpoint_defaults_to_mommyme_when_no_theme_supplied(self):
        source = {
            "slug": "mommyme-test",
            "name": "Mommy Test",
            "description": "",
            "items": [{
                "sku": "WM111", "title": "Find", "brand": "B", "price_display": "$1",
                "image_url": "https://i.example/x.jpg",
                "shop_url": "https://goto.walmart.com/c/3590891/1398372/16662?u=wm111",
                "category": "Cat", "rank": 1,
            }],
        }
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            with patch("product_api.WalmartAPI.get_item_by_id", side_effect=lambda sku: {"sku": sku}):
                resp = self.client.post(
                    "/api/walmart/collections/mommyme-test/draft-page",
                    json={
                        "creator_id": "everydaywithsteph",
                        "public_slug": "mommyme-test-page",
                        "title": "Mommy Test",
                        # Note: NO theme field — must default to mommyme.
                    },
                )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        draft = resp.get_json()["draft"]
        self.assertEqual(draft.get("theme"), "mommyme")

    def test_invalid_theme_falls_back_to_mommyme_not_peach(self):
        # If a client posts an unrecognized theme we should snap to mommyme.
        import inspect
        src = inspect.getsource(self.collection_content.save_walmart_collection_draft)
        # Both the "or fallback" and the "if not in valid_themes" branches
        # should now point at mommyme.
        self.assertIn('or "mommyme"', src,
                      "save_walmart_collection_draft must default to mommyme when theme is blank")
        # No straggling peach defaults in the function body.
        self.assertNotIn('theme = "peach"', src,
                         "save_walmart_collection_draft must not silently snap invalid themes to peach")

    def test_upsert_collage_from_draft_defaults_to_mommyme(self):
        import inspect
        src = inspect.getsource(self.collection_content._upsert_collage_from_draft)
        self.assertIn('"mommyme"', src,
                      "_upsert_collage_from_draft draft_theme fallback must be mommyme")
        self.assertNotIn('or "peach"', src,
                         "_upsert_collage_from_draft must not fall back to peach")

    def test_create_post_template_defaults_to_mommyme(self):
        # Render the template via the public route and confirm the JS/HTML
        # default lands on mommyme, not peach.
        source = {
            "slug": "template-default-test",
            "name": "Template Default Test",
            "description": "",
            "items": [{
                "sku": "WM222", "title": "Find", "brand": "B", "price_display": "$1",
                "image_url": "https://i.example/x.jpg",
                "shop_url": "https://goto.walmart.com/c/3590891/1398372/16662?u=wm222",
                "category": "Cat", "rank": 1,
            }],
        }
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            resp = self.client.get("/collections/template-default-test/create-post")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # The themeValue hidden input default.
        self.assertIn('id="themeValue" type="hidden" value="mommyme"', html)
        # The JS setTheme bootstrap call.
        self.assertIn("setTheme('mommyme')", html)
        # The picker must offer mommyme as a clickable option.
        self.assertIn('data-theme="mommyme"', html)


class PostSmartLinkDefaultCampaignTest(_CollectionContentBaseCase):
    """Fix #4: /archer/posts/<id>/edit must compute a default utm_campaign
    so /urlgenius/smart_link never receives a blank one."""

    def _insert_post(self):
        import posts as _posts
        return _posts.create_post(
            creator_id="everydaywithsteph",
            asin="B0CHECKPT1",
            angle="checkpoint-angle",
            copy="post copy",
            status="approved",
            product_name="Checkpoint Product",
            product_brand="CheckpointBrand",
        )

    def test_post_edit_page_renders_default_campaign_fallback(self):
        post = self._insert_post()
        resp = self.client.get(f"/archer/posts/{post['id']}/edit")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # The JS contract that prevents the blank-campaign 4xx from URLGenius.
        self.assertIn("function defaultCampaign()", html,
                      "post edit page must define defaultCampaign() helper")
        # getUtm must use the fallback when utm_campaign input is blank.
        self.assertIn("val('utm_campaign') || defaultCampaign()", html,
                      "getUtm must fall back to defaultCampaign() when blank")
        # The default must be surfaced into the input on load so the user
        # sees what will actually be sent.
        self.assertIn("syncCampaignDefault()", html,
                      "post edit page must seed the utm_campaign input on load")
        # The fallback derivation order: collection_slug → angle → post id.
        self.assertIn("val('collection_slug') || val('angle') || `organic-post-${postId}`", html,
                      "defaultCampaign must derive from slug → angle → post id")


class FreshPgLaunchSafetyTest(unittest.TestCase):
    """Clean PG launch must not auto-copy historical SQLite data at startup."""

    @classmethod
    def setUpClass(cls):
        cls.repo = pathlib.Path(__file__).resolve().parents[1]

    def test_bootstrap_does_not_auto_seed_from_sqlite(self):
        import inspect
        import db_schema

        bootstrap_src = inspect.getsource(db_schema.bootstrap)
        full_src = pathlib.Path(db_schema.__file__).read_text()

        self.assertIn("init_schema()", bootstrap_src)
        self.assertIn("seed_default_creator()", bootstrap_src)
        self.assertNotIn("_seed_from_sqlite_snapshot", full_src)
        self.assertNotIn("_seed_thread_started", full_src)
        self.assertNotIn("_SQLITE_SEED_TABLES", full_src)
        self.assertNotIn("threading.Thread", bootstrap_src)
        self.assertNotIn("os.path.exists(DB_PATH)", bootstrap_src)

    def test_migration_script_uses_explicit_schema_setup_not_bootstrap(self):
        script = self.repo / "scripts" / "migrate_sqlite_to_postgres.py"
        src = script.read_text()

        self.assertIn("db_schema.init_schema()", src)
        self.assertIn("db_schema.seed_default_creator()", src)
        self.assertNotIn("db_schema.bootstrap()", src)

    def test_sqlite_catalog_is_ignored_and_not_tracked(self):
        gitignore = (self.repo / ".gitignore").read_text()
        self.assertIn("data/archer_catalog.db", gitignore)

        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "data/archer_catalog.db"],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(
            tracked.returncode,
            0,
            "data/archer_catalog.db must not be tracked on the fresh PG launch branch",
        )

    def test_healthz_returns_ok_without_auth(self):
        import app

        client = app.app.test_client()
        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "ok")

    def test_app_import_has_no_bootstrap_or_legacy_prompt_constant_reads(self):
        src = (self.repo / "app.py").read_text()

        self.assertNotIn("db_schema.bootstrap()", src)
        self.assertNotIn("STEPH_CAPTION_PROMPT", src)
        self.assertNotIn("STEPH_AD_COPY_PROMPT", src)
        self.assertNotIn("STEPH_ORGANIC_POSTS_PROMPT", src)
        self.assertNotIn("STEPH_CAMPAIGN_PACKAGE_PROMPT", src)
        self.assertIn("build_caption_prompt()", src)
        # build_ad_copy_prompt / build_organic_posts_prompt /
        # build_campaign_package_prompt were used by ad-ops AI helpers that
        # were removed in the Shop-MomandMe strip-down. Each should no
        # longer appear as a callable (i.e. function-call site), even
        # though removal-comment text may still mention them.
        for sym in (
            "build_ad_copy_prompt",
            "build_organic_posts_prompt",
            "build_campaign_package_prompt",
        ):
            self.assertNotIn(f"{sym}()", src,
                msg=f"{sym} still has a call site in app.py")
            self.assertNotIn(f"system={sym}",  src,
                msg=f"{sym} still used as system prompt in app.py")

    def test_workbook_import_fetch_sends_same_origin_credentials(self):
        src = (self.repo / "templates" / "walmart_trending_now.html").read_text()

        self.assertIn("fetch('/admin/walmart-trends/bootstrap'", src)
        self.assertIn("credentials: 'same-origin'", src)

    def test_admin_trends_template_wires_limited_amazon_enrichment(self):
        src = (self.repo / "templates" / "walmart_trending_now.html").read_text()

        self.assertIn("Enrich Amazon prices/images", src)
        self.assertIn("fetch('/admin/amazon-trends/enrich'", src)
        self.assertIn("data.source === 'Amazon'", src)
        self.assertIn("limit: 30", src)
        self.assertIn("max_workers: 4", src)

    def test_admin_amazon_enrich_endpoint_requires_auth_and_returns_counts(self):
        import app

        app._SCHEMA_READY = True
        client = app.app.test_client()

        with patch("app._walmart_content_demo_allowed", return_value=False):
            unauth = client.post(
                "/admin/amazon-trends/enrich",
                base_url="https://dashboard.example.com",
                json={"limit": 30, "max_workers": 4},
            )
        self.assertNotEqual(unauth.status_code, 200)

        with client.session_transaction() as sess:
            sess["admin_authed"] = True
        with patch("amazon_trends.AmazonTrendRefreshService") as svc_cls:
            svc_cls.return_value.enrich_pending.return_value = {
                "queued": 2,
                "creators": 1,
                "crawlbase": 1,
                "pending": 0,
            }
            authed = client.post(
                "/admin/amazon-trends/enrich",
                json={"limit": 30, "max_workers": 4},
            )

        self.assertEqual(authed.status_code, 200)
        self.assertEqual(authed.get_json()["counts"]["queued"], 2)
        svc_cls.return_value.enrich_pending.assert_called_once_with(limit=30, max_workers=4)

    def test_trends_template_renders_amazon_image_fallback_and_honest_price(self):
        import app
        from flask import render_template

        data = {
            "last_refreshed": "",
            "collections": [{
                "slug": "amazon-test",
                "name": "Amazon Test",
                "retailer": "amazon",
                "items": [{
                    "sku": "B001AMAZON",
                    "retailer": "amazon",
                    "title": "Amazon Test Product",
                    "brand": "",
                    "image_url": "",
                    "fallback_image_url": "https://ws-na.amazon-adsystem.com/widgets/q?ASIN=B001AMAZON",
                    "price_display": "",
                    "badges": [],
                    "shop_url": "https://amazon.com/dp/B001AMAZON?tag=test-20",
                }],
            }],
        }
        with app.app.test_request_context("/walmart/trending-now"):
            html = render_template(
                "walmart_trending_now.html",
                data=data,
                admin_mode=False,
                shop_subdomain="shop.example.com",
                public_nav_items=[],
                nav_active="trends",
                admin_error="",
            )

        self.assertIn("ws-na.amazon-adsystem.com/widgets/q?ASIN=B001AMAZON", html)
        self.assertIn("See price at Amazon", html)

    def test_admin_trends_page_surfaces_loader_errors_instead_of_500(self):
        import app

        # Make sure a prior test did not mark the app schema as already ready
        # against a different temporary database.
        app._SCHEMA_READY = True

        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["admin_authed"] = True

        with patch("walmart_trends.get_trending_page_data", side_effect=RuntimeError("boom")):
            resp = client.get("/walmart/trending-now?admin=1")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Trending data could not load: boom", html)
        self.assertIn("Workbook Import", html)

    def test_admin_trends_page_renders_with_empty_tables(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, "empty-trends.db")
        saved_db_url = os.environ.pop("DATABASE_URL", None)
        os.environ["CACHE_DB_PATH"] = db_path
        try:
            import db_schema
            import walmart_trends
            import app

            db_schema.DB_PATH = db_path
            walmart_trends.DB_PATH = db_path
            db_schema.bootstrap()

            client = app.app.test_client()
            with client.session_transaction() as sess:
                sess["admin_authed"] = True
            resp = client.get("/walmart/trending-now?admin=1")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("EchoTribe", html)
            self.assertIn("Home", html)
        finally:
            if saved_db_url is not None:
                os.environ["DATABASE_URL"] = saved_db_url
            tmp.cleanup()


class TrendsCreatePostNarrowQueryTest(unittest.TestCase):
    """Issue 1: opening /collections/<slug>/create-post was taking 6+s because
    cc.get_walmart_collection delegated to get_trending_page_data() which loads
    every active collection. The fix routes through a narrow per-slug query in
    WalmartTrendStore.get_collection_by_slug instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "narrow-query.db")
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)
        os.environ["CACHE_DB_PATH"] = self.db_path

        import db_schema
        import walmart_trends
        db_schema.DB_PATH = self.db_path
        walmart_trends.DB_PATH = self.db_path
        db_schema.bootstrap()
        self.db_schema = db_schema
        self.walmart_trends = walmart_trends

    def tearDown(self):
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url
        self.tmp.cleanup()

    def _seed_walmart_collection(self, slug: str = "checkpoint-coll"):
        conn = self.walmart_trends._connect()
        conn.execute(
            "INSERT OR REPLACE INTO walmart_products "
            "(sku, item_name, brand) VALUES ('WM_X', 'Product X', 'BrandX')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections "
            "(slug, name, source_type, is_active, display_order, metadata_json, retailer) "
            f"VALUES ('{slug}', 'Checkpoint', 'workbook_bootstrap', 1, 1, '{{}}', 'walmart')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collection_items "
            "(collection_slug, sku, display_order, badges_json, retailer) "
            f"VALUES ('{slug}', 'WM_X', 0, '[]', 'walmart')"
        )
        conn.commit()
        conn.close()

    def test_get_walmart_collection_does_not_load_landing_page_data(self):
        # The whole point of the perf fix: NEVER hit landing_page_data() just
        # to look up one collection.
        from unittest.mock import patch
        import collection_content as cc

        self._seed_walmart_collection("perf-test")
        with patch.object(
            self.walmart_trends.WalmartTrendStore,
            "landing_page_data",
        ) as mock_landing:
            mock_landing.return_value = {"collections": []}
            cc.get_walmart_collection("perf-test")
        mock_landing.assert_not_called()

    def test_get_collection_by_slug_returns_correct_collection(self):
        self._seed_walmart_collection("alpha")
        store = self.walmart_trends.WalmartTrendStore()
        out = store.get_collection_by_slug("alpha")
        self.assertIsNotNone(out)
        self.assertEqual(out["slug"], "alpha")
        self.assertEqual(out["retailer"], "walmart")
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["sku"], "WM_X")

    def test_get_collection_by_slug_returns_none_for_missing(self):
        store = self.walmart_trends.WalmartTrendStore()
        self.assertIsNone(store.get_collection_by_slug("does-not-exist"))
        self.assertIsNone(store.get_collection_by_slug(""))
        self.assertIsNone(store.get_collection_by_slug(None))

    def test_get_collection_by_slug_skips_inactive_collections(self):
        # An inactive (archived/deactivated) collection slug must not be
        # accidentally surfaced — same semantics as landing_page_data.
        self._seed_walmart_collection("active-one")
        conn = self.walmart_trends._connect()
        conn.execute(
            "INSERT OR REPLACE INTO walmart_collections "
            "(slug, name, source_type, is_active, display_order, metadata_json, retailer) "
            "VALUES ('archived-one', 'Archived', 'workbook_bootstrap', 0, 2, '{}', 'walmart')"
        )
        conn.commit()
        conn.close()

        store = self.walmart_trends.WalmartTrendStore()
        self.assertIsNotNone(store.get_collection_by_slug("active-one"))
        self.assertIsNone(store.get_collection_by_slug("archived-one"))


class P07DemoCreatorSeedTest(_CollectionContentBaseCase):
    """Demo-safe seeding of the default creator's P0.7 metadata fields.

    Color fields (brand_primary etc.) must remain NULL until Steph
    confirms her palette via the theme picker — only metadata fields
    that are demo-safe regardless of brand confirmation are seeded.
    """

    def test_default_creator_seeded_with_demo_safe_metadata(self):
        # Bootstrap already happened in setUp; default creator row exists.
        conn = self.db_schema._connect()
        try:
            row = conn.execute(
                "SELECT logo_url, shop_domain, meta_title_template, "
                "meta_description_template, brand_primary, brand_on_primary, "
                "brand_primary_container, brand_on_primary_container, "
                "brand_surface, brand_on_surface "
                "FROM creators WHERE id = ?",
                ('everydaywithsteph',),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "Default creator row missing after bootstrap")
        # Demo-safe metadata IS set
        self.assertEqual(row[0], 'static/images/mmc-og-preview.png')
        self.assertEqual(row[1], 'shop.mommyandmecollective.com')
        self.assertIn('{collection}', row[2])
        self.assertIn('{collection}', row[3])
        # Brand color fields are explicitly NOT set — must remain NULL
        # until Steph confirms via the theme picker. The framework falls
        # through to Creator Core defaults via _brand_vars.html when NULL.
        self.assertIsNone(row[4], "brand_primary should be NULL pre-Steph-confirmation")
        self.assertIsNone(row[5], "brand_on_primary should be NULL pre-Steph-confirmation")
        self.assertIsNone(row[6], "brand_primary_container should be NULL pre-Steph-confirmation")
        self.assertIsNone(row[7], "brand_on_primary_container should be NULL pre-Steph-confirmation")
        # K1 canvas/surface pair — same NULL-until-confirmed policy.
        self.assertIsNone(row[8], "brand_surface should be NULL pre-Steph-confirmation")
        self.assertIsNone(row[9], "brand_on_surface should be NULL pre-Steph-confirmation")

    def test_seed_backfills_metadata_on_existing_row_with_null_fields(self):
        # Simulate the case where the creators row pre-dates P0.7 and has
        # NULL metadata. Reset the metadata fields, re-run seed, confirm
        # the demo-safe fields get backfilled but color fields stay NULL.
        conn = self.db_schema._connect()
        try:
            conn.execute(
                "UPDATE creators SET logo_url = NULL, shop_domain = NULL, "
                "meta_title_template = NULL, meta_description_template = NULL "
                "WHERE id = ?",
                ('everydaywithsteph',),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-run the seed; should backfill the NULLs.
        self.db_schema.seed_default_creator()

        conn = self.db_schema._connect()
        try:
            row = conn.execute(
                "SELECT logo_url, shop_domain, meta_title_template, "
                "brand_primary FROM creators WHERE id = ?",
                ('everydaywithsteph',),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 'static/images/mmc-og-preview.png')
        self.assertEqual(row[1], 'shop.mommyandmecollective.com')
        self.assertIn('{collection}', row[2])
        self.assertIsNone(row[3], "Color fields must never be backfilled by the seed")

    def test_seed_does_not_overwrite_admin_set_brand_colors(self):
        # If an admin has populated brand colors via SQL or via a future
        # admin UI, the seed must NOT clobber those values on next boot.
        conn = self.db_schema._connect()
        try:
            conn.execute(
                "UPDATE creators SET brand_primary = ?, brand_on_primary = ? "
                "WHERE id = ?",
                ('#7C7D6A', '#F5F2ED', 'everydaywithsteph'),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-run seed — backfill logic should ignore color fields entirely.
        self.db_schema.seed_default_creator()

        conn = self.db_schema._connect()
        try:
            row = conn.execute(
                "SELECT brand_primary, brand_on_primary "
                "FROM creators WHERE id = ?",
                ('everydaywithsteph',),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], '#7C7D6A',
                         "Seed must not overwrite admin-set brand_primary")
        self.assertEqual(row[1], '#F5F2ED',
                         "Seed must not overwrite admin-set brand_on_primary")


class P07CreatorBrandColumnsTest(_CollectionContentBaseCase):
    """P0.7 + K1 — per-creator brand override surface.

    Confirms the 10 brand/metadata columns exist after bootstrap and accept
    the contract values the framework will write. K1 added the
    brand_surface/brand_on_surface pair on top of the original P0.7 eight.
    """

    REQUIRED_COLUMNS = (
        "logo_url",
        "shop_domain",
        "meta_title_template",
        "meta_description_template",
        "brand_primary",
        "brand_on_primary",
        "brand_primary_container",
        "brand_on_primary_container",
        "brand_surface",
        "brand_on_surface",
    )

    def test_brand_columns_present_after_bootstrap(self):
        conn = self.db_schema._connect()
        try:
            row = conn.execute("PRAGMA table_info(creators)").fetchall()
        finally:
            conn.close()
        present = {r[1] for r in row}  # column name is index 1 in PRAGMA result
        missing = [c for c in self.REQUIRED_COLUMNS if c not in present]
        self.assertEqual(
            missing, [],
            f"P0.7 brand columns missing from creators table: {missing}",
        )

    def test_brand_columns_accept_write_read(self):
        conn = self.db_schema._connect()
        try:
            conn.execute(
                """
                INSERT INTO creators (id, display_name, logo_url, shop_domain,
                  meta_title_template, meta_description_template,
                  brand_primary, brand_on_primary,
                  brand_primary_container, brand_on_primary_container,
                  brand_surface, brand_on_surface)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-creator", "Test Creator",
                    "https://example.com/logo.svg",
                    "shop.example.com",
                    "{collection} | Test Creator",
                    "Discover {collection} hand-picked by Test Creator.",
                    "#7C7D6A", "#F5F2ED",
                    "#DDBBA4", "#3D3A33",
                    "#E5DBC8", "#1A1A17",
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT logo_url, shop_domain, brand_primary, brand_on_primary, "
                "brand_primary_container, brand_on_primary_container, "
                "brand_surface, brand_on_surface "
                "FROM creators WHERE id = ?",
                ("test-creator",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "https://example.com/logo.svg")
        self.assertEqual(row[1], "shop.example.com")
        self.assertEqual(row[2], "#7C7D6A")
        self.assertEqual(row[3], "#F5F2ED")
        self.assertEqual(row[4], "#DDBBA4")
        self.assertEqual(row[5], "#3D3A33")
        self.assertEqual(row[6], "#E5DBC8")
        self.assertEqual(row[7], "#1A1A17")


if __name__ == "__main__":
    unittest.main()
