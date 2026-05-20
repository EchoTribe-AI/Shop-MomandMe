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
        # Server-side admin auth landed in feature/pg-launch — admin pages
        # now redirect to /admin/login unless the session is authed. These
        # tests exercise admin endpoints, so seed an authed session.
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

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
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertIsNone(conn.execute(
                "SELECT slug FROM collages WHERE slug = ?",
                ("walmart-kids-room-character-favorites",),
            ).fetchone())
        finally:
            conn.close()
        preview = self.client.get("/shop/walmart-kids-room-character-favorites?preview=1")
        self.assertEqual(preview.status_code, 200)
        preview_html = preview.get_data(as_text=True)
        self.assertIn("Character Sheet Set 1", preview_html)
        self.assertIn("$7.77", preview_html)

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

    def test_create_post_ui_hydrates_full_source_snapshot_when_no_draft_exists(self):
        with patch.object(self.collection_content, "get_walmart_collection", return_value=_walmart_collection(12)):
            resp = self.client.get("/collections/kids-room-character-favorites/create-post")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Character Sheet Set 12", html)
        self.assertIn("Add Amazon ASIN/URL or Walmart SKU/URL", html)

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

        # Nav hrefs are now relative (was https://shop.echotribe.ai/* before
        # feature/public-nav-relative-and-header-logo). Relative paths work
        # on whichever host serves the response, which is what every
        # multi-deploy storefront needs.
        # Plan §7: 'Social Posts' nav item hidden from the public nav.
        # The /shop/posts route still exists; only the visible nav link
        # was dropped. Add /posts assertion back when posts ships.
        for html in (landing, posts, directory):
            self.assertIn('href="/collections"', html)
            self.assertIn('href="/trends"', html)

        collections = self.client.get("/collections", headers={"Host": "shop.echotribe.ai"})
        self.assertEqual(collections.status_code, 200)
        self.assertIn("Shop The Mommy &amp; Me Collective", collections.get_data(as_text=True))

        with patch("walmart_trends.get_trending_page_data", return_value={"last_refreshed": "Today", "collections": []}):
            trends = self.client.get("/trends", headers={"Host": "shop.echotribe.ai"})
        self.assertEqual(trends.status_code, 200)
        self.assertIn("What’s Trending Now", trends.get_data(as_text=True))

    def test_trends_cards_never_render_contaminated_walmart_brand(self):
        import walmart_trends

        walmart_trends.DB_PATH = self.db_path
        store = walmart_trends.WalmartTrendStore()
        run_id = store.create_run("workbook_bootstrap")
        store.finish_run(run_id, "success", {"records": 1}, [])
        store.replace_collections(run_id, "workbook_bootstrap", [{
            "slug": "top-sellers",
            "name": "Top Sellers",
            "items": [{"sku": "18985723227", "badges": []}],
        }])
        store.upsert_product_from_record(walmart_trends.TrendRecord(
            sku="18985723227",
            item_name="CONCETTA 4-Piece Patio Furniture Set with Loveseat",
            brand="WalmartCreator.com",
        ))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE walmart_products SET brand = ? WHERE sku = ?",
                ("WalmartCreator.com", "18985723227"),
            )
            conn.commit()
        finally:
            conn.close()

        public_html = self.client.get("/trends", headers={"Host": "shop.echotribe.ai"}).get_data(as_text=True)
        admin_html = self.client.get("/walmart/trending-now?admin=1").get_data(as_text=True)

        self.assertNotIn("WalmartCreator.com", public_html)
        self.assertNotIn("WALMARTCREATOR.COM", public_html)
        self.assertNotIn("WalmartCreator.com", admin_html)
        self.assertNotIn("WALMARTCREATOR.COM", admin_html)
        self.assertIn("CONCETTA", public_html)
        self.assertIn("CONCETTA", admin_html)

    def test_admin_header_links_distinguish_hub_create_and_manage(self):
        hub_html = self.client.get("/hub").get_data(as_text=True)
        self.assertIn('href="/hub" class="tb-link active"', hub_html)
        self.assertIn('href="/walmart/trending-now?admin=1" class="tb-link"', hub_html)
        self.assertIn('href="/admin/posts" class="tb-link"', hub_html)
        self.assertIn("Content Hub", hub_html)

        with patch("walmart_trends.get_trending_page_data", return_value={"last_refreshed": "Today", "collections": []}):
            create_html = self.client.get("/walmart/trending-now?admin=1").get_data(as_text=True)
        self.assertIn('href="/hub" class="tb-link active"', create_html)
        self.assertIn('href="/walmart/trending-now?admin=1" class="tb-link"', create_html)
        self.assertIn('href="/admin/posts" class="tb-link"', create_html)

    def test_collection_editor_renders_mobile_publishing_workflow(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Mobile Workflow Title",
                "landing_intro": "Mobile workflow intro.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        self.assertEqual(self.client.post(f"/api/collection-content-drafts/{draft_id}/publish").status_code, 200)

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            editor = self.client.get("/collections/walmart-kids-room-character-favorites/edit")
        self.assertEqual(editor.status_code, 200)
        html = editor.get_data(as_text=True)
        self.assertIn("Edit Page", html)
        self.assertIn("Quick actions", html)
        self.assertIn("Publishing", html)
        self.assertIn("Save changes", html)
        self.assertIn("Move to draft", html)
        self.assertIn("More content tools", html)
        self.assertNotIn("Page status", html)
        self.assertNotIn("Apply status", html)

    def test_draft_product_snapshot_drives_reload_preview_publish_and_unpublish(self):
        source = _walmart_collection(4)
        edited = [
            {
                "asin": "WM003",
                "product_name": "Moved First Walmart",
                "brand": "WalmartBrand",
                "price_display": "$10.00",
                "image_encoded_string": "https://i.example/3.jpg",
                "attribution_link": "https://goto.walmart.com/c/3590891/1398372/16662?u=wm3",
                "retailer": "Walmart",
                "retailer_name": "Walmart",
                "network": "walmart",
                "rank": 1,
            },
            {
                "asin": "B0AMZN0001",
                "product_name": "Added Amazon Find",
                "brand": "AmazonBrand",
                "price_display": "$22.00",
                "image_encoded_string": "https://i.example/amazon.jpg",
                "attribution_link": "https://urlgeni.us/amazon/added",
                "retailer": "Amazon",
                "retailer_name": "Amazon",
                "network": "amazon",
                "rank": 2,
            },
            {
                "asin": "WM002",
                "product_name": "Kept Second Walmart",
                "brand": "WalmartBrand",
                "price_display": "$10.00",
                "image_encoded_string": "https://i.example/2.jpg",
                "attribution_link": "https://goto.walmart.com/c/3590891/1398372/16662?u=wm2",
                "retailer": "Walmart",
                "retailer_name": "Walmart",
                "network": "walmart",
                "rank": 3,
            },
        ]
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "creator_id": "everydaywithsteph",
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Edited Kids Room",
                "landing_intro": "Edited intro.",
                "product_snapshot": edited,
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]

        conn = sqlite3.connect(self.db_path)
        try:
            products = json.loads(conn.execute(
                "SELECT product_snapshot_json FROM collection_content_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()[0])
            self.assertEqual([p["asin"] for p in products], ["WM003", "B0AMZN0001", "WM002"])
            self.assertIsNone(conn.execute(
                "SELECT slug FROM collages WHERE slug = ?",
                ("walmart-kids-room-character-favorites",),
            ).fetchone())
        finally:
            conn.close()

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            editor = self.client.get("/collections/walmart-kids-room-character-favorites/edit")
        self.assertEqual(editor.status_code, 200)
        html = editor.get_data(as_text=True)
        self.assertIn("Moved First Walmart", html)
        self.assertIn("Added Amazon Find", html)
        self.assertNotIn("Character Sheet Set 1", html)

        preview = self.client.get("/shop/walmart-kids-room-character-favorites?preview=1")
        self.assertEqual(preview.status_code, 200)
        preview_html = preview.get_data(as_text=True)
        self.assertIn("Moved First Walmart", preview_html)
        self.assertIn("Added Amazon Find", preview_html)
        self.assertNotIn("Character Sheet Set 1", preview_html)

        publish_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/publish")
        self.assertEqual(publish_resp.status_code, 200)
        public = self.client.get("/shop/walmart-kids-room-character-favorites")
        self.assertEqual(public.status_code, 200)
        public_html = public.get_data(as_text=True)
        self.assertIn("Moved First Walmart", public_html)
        self.assertIn("Added Amazon Find", public_html)

        edited_after_publish = [
            {**edited[2], "rank": 1},
            {**edited[1], "rank": 2},
        ]
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            save_after_publish = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "draft_id": draft_id,
                "creator_id": "everydaywithsteph",
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Edited Kids Room Again",
                "landing_intro": "Draft-only edit.",
                "product_snapshot": edited_after_publish,
            })
        self.assertEqual(save_after_publish.status_code, 200)
        public_before_republish = self.client.get("/shop/walmart-kids-room-character-favorites").get_data(as_text=True)
        self.assertIn("Moved First Walmart", public_before_republish)
        self.assertNotIn("Draft-only edit.", public_before_republish)

        republish_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/publish")
        self.assertEqual(republish_resp.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        try:
            duplicate_count = conn.execute(
                "SELECT COUNT(*) FROM collages WHERE slug = ?",
                ("walmart-kids-room-character-favorites",),
            ).fetchone()[0]
            public_products = json.loads(conn.execute(
                "SELECT products_json FROM collages WHERE slug = ?",
                ("walmart-kids-room-character-favorites",),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(duplicate_count, 1)
        self.assertEqual([p["asin"] for p in public_products], ["WM002", "B0AMZN0001"])

        unpublish_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/unpublish")
        self.assertEqual(unpublish_resp.status_code, 200)
        self.assertEqual(self.client.get("/shop/walmart-kids-room-character-favorites").status_code, 404)
        self.assertEqual(self.client.get("/shop/walmart-kids-room-character-favorites?preview=1").status_code, 200)

    def test_public_slug_edit_loads_newest_draft_not_old_published_content(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            first = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Original Published Title",
                "landing_intro": "Original published intro.",
            })
        self.assertEqual(first.status_code, 200)
        draft_id = first.get_json()["draft_id"]
        self.assertEqual(self.client.post(f"/api/collection-content-drafts/{draft_id}/publish").status_code, 200)

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            second = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Newest Draft Title",
                "landing_intro": "Newest draft intro.",
            })
        self.assertEqual(second.status_code, 200)

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            editor = self.client.get("/collections/walmart-kids-room-character-favorites/edit")
        self.assertEqual(editor.status_code, 200)
        html = editor.get_data(as_text=True)
        self.assertIn("Newest Draft Title", html)
        self.assertNotIn("Original Published Title", html)

    def test_wrong_route_slug_with_draft_id_preserves_source_slug_and_generation_context(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Editable Page",
                "landing_intro": "Editable intro.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]

        def source_only(slug):
            return source if slug == "kids-room-character-favorites" else None

        with patch.object(self.collection_content, "get_walmart_collection", side_effect=source_only):
            save_resp = self.client.post("/api/walmart/collections/walmart-kids-room-character-favorites/draft-page", json={
                "draft_id": draft_id,
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Saved Through Public Slug Route",
                "landing_intro": "Saved intro.",
            })
            generate_resp = self.client.post("/api/walmart/collections/walmart-kids-room-character-favorites/generate-post", json={
                "draft_id": draft_id,
                "voice_source_text": "These are still the same source finds.",
            })
        self.assertEqual(save_resp.status_code, 200)
        self.assertEqual(generate_resp.status_code, 400)
        self.assertIn("AI key missing", generate_resp.get_json()["error"])

        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT source_collection_slug, source_collection_id FROM collection_content_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "kids-room-character-favorites")
        self.assertEqual(row[1], "kids-room-character-favorites")

    def test_archiving_public_page_updates_draft_state_and_blocks_stale_republish(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Archive Me",
                "landing_intro": "Archive intro.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        self.assertEqual(self.client.post(f"/api/collection-content-drafts/{draft_id}/publish").status_code, 200)

        archive_resp = self.client.post("/api/collections/archive", json={
            "slug": "walmart-kids-room-character-favorites",
        })
        self.assertEqual(archive_resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        try:
            collage_status = conn.execute(
                "SELECT status FROM collages WHERE slug = ?",
                ("walmart-kids-room-character-favorites",),
            ).fetchone()[0]
            draft_status = conn.execute(
                "SELECT status FROM collection_content_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(collage_status, "archived")
        self.assertEqual(draft_status, "archived")

        republish_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/publish")
        self.assertEqual(republish_resp.status_code, 400)
        self.assertIn("Archived page", republish_resp.get_json()["error"])

    def test_republish_after_edit_updates_public_page_with_current_draft(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Original Live Title",
                "landing_intro": "Original live intro.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        self.assertEqual(self.client.post(f"/api/collection-content-drafts/{draft_id}/publish").status_code, 200)

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            save_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "draft_id": draft_id,
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Republished Current Title",
                "landing_intro": "Republished current intro.",
            })
        self.assertEqual(save_resp.status_code, 200)
        before_republish = self.client.get("/shop/walmart-kids-room-character-favorites").get_data(as_text=True)
        self.assertIn("Original Live Title", before_republish)
        self.assertNotIn("Republished Current Title", before_republish)

        republish_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/publish")
        self.assertEqual(republish_resp.status_code, 200)
        after_republish = self.client.get("/shop/walmart-kids-room-character-favorites").get_data(as_text=True)
        self.assertIn("Republished Current Title", after_republish)
        self.assertIn("Republished current intro.", after_republish)

    def test_manage_publish_uses_latest_collection_editor_draft(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Manage Original Title",
                "landing_intro": "Manage original intro.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        self.assertEqual(self.client.post(f"/api/collection-content-drafts/{draft_id}/publish").status_code, 200)

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            save_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "draft_id": draft_id,
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Manage Current Draft Title",
                "landing_intro": "Manage current draft intro.",
                "status": "draft",
            })
        self.assertEqual(save_resp.status_code, 200)
        public_before = self.client.get("/shop/walmart-kids-room-character-favorites").get_data(as_text=True)
        self.assertIn("Manage Original Title", public_before)
        self.assertNotIn("Manage Current Draft Title", public_before)

        manage_publish = self.client.post("/api/collections/publish", json={
            "slug": "walmart-kids-room-character-favorites",
        })
        self.assertEqual(manage_publish.status_code, 200)
        public_after = self.client.get("/shop/walmart-kids-room-character-favorites").get_data(as_text=True)
        self.assertIn("Manage Current Draft Title", public_after)
        self.assertIn("Manage current draft intro.", public_after)

    def test_unpublish_to_draft_hides_public_page_but_keeps_preview(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Draft Toggle Title",
                "landing_intro": "Draft toggle intro.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        self.assertEqual(self.client.post(f"/api/collection-content-drafts/{draft_id}/publish").status_code, 200)

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            save_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "draft_id": draft_id,
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Hidden Draft Title",
                "landing_intro": "Hidden draft intro.",
                "status": "draft",
            })
        self.assertEqual(save_resp.status_code, 200)
        unpublish_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/unpublish")
        self.assertEqual(unpublish_resp.status_code, 200)
        self.assertEqual(self.client.get("/shop/walmart-kids-room-character-favorites").status_code, 404)
        preview = self.client.get("/shop/walmart-kids-room-character-favorites?preview=1")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Hidden Draft Title", preview.get_data(as_text=True))

    def test_archive_draft_endpoint_hides_public_and_edit_loads_archived_state(self):
        source = _walmart_collection(2)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Archive Endpoint Title",
                "landing_intro": "Archive endpoint intro.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        self.assertEqual(self.client.post(f"/api/collection-content-drafts/{draft_id}/publish").status_code, 200)

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            save_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "draft_id": draft_id,
                "public_slug": "walmart-kids-room-character-favorites",
                "title": "Archived Current Title",
                "landing_intro": "Archived current intro.",
                "status": "archived",
            })
        self.assertEqual(save_resp.status_code, 200)
        archive_resp = self.client.post(f"/api/collection-content-drafts/{draft_id}/archive")
        self.assertEqual(archive_resp.status_code, 200)
        self.assertEqual(self.client.get("/shop/walmart-kids-room-character-favorites").status_code, 404)
        preview = self.client.get("/shop/walmart-kids-room-character-favorites?preview=1")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Archived Current Title", preview.get_data(as_text=True))

        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            editor = self.client.get("/collections/walmart-kids-room-character-favorites/edit")
        self.assertEqual(editor.status_code, 200)
        self.assertIn("Archived Current Title", editor.get_data(as_text=True))

        manage = self.client.get("/admin/posts")
        self.assertEqual(manage.status_code, 200)
        manage_html = manage.get_data(as_text=True)
        self.assertIn("archived", manage_html)
        self.assertIn("Restore", manage_html)

    def test_add_product_accepts_amazon_and_walmart_inputs_with_retailer_metadata(self):
        self.assertEqual(self.app_module._parse_collection_product_input("B0AMZN0002"), ("amazon", "B0AMZN0002"))
        self.assertEqual(
            self.app_module._parse_collection_product_input("https://www.amazon.com/example/dp/b0amzn0002?tag=old"),
            ("amazon", "B0AMZN0002"),
        )
        self.assertEqual(self.app_module._parse_collection_product_input("987654321"), ("walmart", "987654321"))
        self.assertEqual(
            self.app_module._parse_collection_product_input("https://www.walmart.com/ip/Example-Product/987654321"),
            ("walmart", "987654321"),
        )
        source = _walmart_collection(1)
        with patch.object(self.collection_content, "get_walmart_collection", return_value=source):
            draft_resp = self.client.post("/api/walmart/collections/kids-room-character-favorites/draft-page", json={
                "public_slug": "walmart-kids-room-character-favorites",
                "landing_intro": "Fresh finds.",
            })
        self.assertEqual(draft_resp.status_code, 200)
        draft_id = draft_resp.get_json()["draft_id"]
        with patch("app._build_amazon_snapshot_product", return_value={
            "asin": "B0AMZN0002",
            "product_name": "Amazon URL Product",
            "attribution_link": "https://urlgeni.us/amazon/url-product",
            "retailer": "Amazon",
            "retailer_name": "Amazon",
            "network": "amazon",
        }) as amazon_build, patch("app._build_walmart_snapshot_product", return_value={
            "asin": "987654321",
            "product_name": "Walmart URL Product",
            "attribution_link": "https://urlgeni.us/walmart/url-product",
            "retailer": "Walmart",
            "retailer_name": "Walmart",
            "network": "walmart",
        }) as walmart_build:
            amazon_resp = self.client.post(
                f"/api/walmart/collections/kids-room-character-favorites/drafts/{draft_id}/add-product",
                json={"product": "https://www.amazon.com/example/dp/B0AMZN0002?tag=old"},
            )
            walmart_resp = self.client.post(
                f"/api/walmart/collections/kids-room-character-favorites/drafts/{draft_id}/add-product",
                json={"product": "https://www.walmart.com/ip/Example-Product/987654321"},
            )
        self.assertEqual(amazon_resp.status_code, 200)
        self.assertEqual(walmart_resp.status_code, 200)
        amazon_build.assert_called_once_with("B0AMZN0002")
        walmart_build.assert_called_once_with("987654321")
        products = walmart_resp.get_json()["products"]
        self.assertEqual(products[-2]["network"], "amazon")
        self.assertEqual(products[-1]["network"], "walmart")

        conn = sqlite3.connect(self.db_path)
        try:
            stored = json.loads(conn.execute(
                "SELECT product_snapshot_json FROM collection_content_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(stored[-2]["asin"], "B0AMZN0002")
        self.assertEqual(stored[-1]["asin"], "987654321")

    def test_draft_snapshot_edits_do_not_mutate_source_trend_tables(self):
        import walmart_trends

        walmart_trends.DB_PATH = self.db_path
        store = walmart_trends.WalmartTrendStore()
        run_id = store.create_run("workbook_bootstrap")
        store.finish_run(run_id, "success", {"records": 2}, [])
        store.upsert_product_from_record(walmart_trends.TrendRecord(sku="WM001", item_name="Source First"))
        store.upsert_product_from_record(walmart_trends.TrendRecord(sku="WM002", item_name="Source Second"))
        store.save_affiliate_link("WM001", "https://www.walmart.com/ip/WM001", "https://goto.walmart.com/source-1", "active")
        store.save_affiliate_link("WM002", "https://www.walmart.com/ip/WM002", "https://goto.walmart.com/source-2", "active")
        store.replace_collections(run_id, "workbook_bootstrap", [{
            "slug": "source-collection",
            "name": "Source Collection",
            "items": [
                {"sku": "WM001", "badges": [], "item_count": 1},
                {"sku": "WM002", "badges": [], "item_count": 2},
            ],
        }])
        edited = [{
            "asin": "WM002",
            "product_name": "Edited Draft Second",
            "attribution_link": "https://goto.walmart.com/source-2",
            "retailer": "Walmart",
            "network": "walmart",
        }]

        draft_resp = self.client.post("/api/walmart/collections/source-collection/draft-page", json={
            "public_slug": "source-collection-page",
            "landing_intro": "Draft snapshot only.",
            "product_snapshot": edited,
        })
        self.assertEqual(draft_resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            source_rows = conn.execute(
                """
                SELECT sku, display_order
                FROM walmart_collection_items
                WHERE collection_slug = ?
                ORDER BY display_order ASC
                """,
                ("source-collection",),
            ).fetchall()
            product_count = conn.execute("SELECT COUNT(*) FROM walmart_products").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual([(row["sku"], row["display_order"]) for row in source_rows], [("WM001", 1), ("WM002", 2)])
        self.assertEqual(product_count, 2)

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

        collage_resp = self.client.get("/api/collections/walmart-kids-room-character-favorites")
        self.assertEqual(collage_resp.status_code, 200)
        collage = collage_resp.get_json()["collage"]
        self.assertEqual(collage["editor_type"], "trend_collection")
        self.assertEqual(collage["edit_url"], "/collections/walmart-kids-room-character-favorites/edit")
        self.assertEqual(len(collage["products"]), 12)

        generic_save = self.client.post("/api/collections/draft", json={
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
        self.assertIn("Character Sheet Set 12", html)
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
