"""P0.2 Insights rebuild — PR 1 tests.

Covers the new 4-section v2 dashboard:
    1. Data-layer helpers in insights.py rank rows correctly and stay
       scoped to creator_id.
    2. The /insights route renders insights_v2.html when
       INSIGHTS_V2_ENABLED is set, and renders the legacy insights.html
       (with tabs intact) when the flag is off.
    3. Default window in the v2 path is 7d.

Pattern mirrors tests/test_storefront_framework_boundary.py: isolated tmp
SQLite DB seeded via raw inserts so we control click_log / collages /
posts / earnings_amazon fixture rows without touching the production DB.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock


_TODAY = date.today()
_YESTERDAY = (_TODAY - timedelta(days=1)).isoformat()
_INSIDE_WINDOW = _YESTERDAY  # within both 7d and 30d defaults


class _InsightsTestBase(unittest.TestCase):
    """Shared fixture: isolated DB + clean env + insights module under test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'insights.db')
        os.environ['CACHE_DB_PATH'] = self.db_path
        os.environ.pop('ACTIVE_CREATOR_ID', None)
        os.environ.pop('INSIGHTS_V2_ENABLED', None)

        # Admin credentials so /insights doesn't 503 in this test process.
        os.environ.setdefault('SECRET_KEY', 'test-secret')
        os.environ.setdefault('ADMIN_PASSWORD', 'test-password')

        import db_schema
        import app
        import insights as insights_mod
        self.db_schema = db_schema
        self.app_module = app
        self.insights = insights_mod

        db_schema.DB_PATH = self.db_path
        db_schema.bootstrap()
        # Prevent app from re-bootstrapping against a stale path.
        app._SCHEMA_READY = True

    def tearDown(self):
        os.environ.pop('CACHE_DB_PATH', None)
        os.environ.pop('INSIGHTS_V2_ENABLED', None)
        os.environ.pop('ACTIVE_CREATOR_ID', None)
        self.tmp.cleanup()

    # ── fixture helpers ────────────────────────────────────────────────
    def _seed_collage(self, slug, creator_id, hero_title=None,
                      status='published', theme='coral'):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO collages "
                "(slug, products_json, creator_id, status, hero_title, theme, click_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (slug, json.dumps([]), creator_id, status,
                 hero_title or slug.replace('-', ' ').title(), theme),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_clicks(self, slug, n, when=_INSIDE_WINDOW):
        """Insert n click_log rows for `slug` dated `when` (YYYY-MM-DD)."""
        conn = sqlite3.connect(self.db_path)
        try:
            for _ in range(n):
                conn.execute(
                    "INSERT INTO click_log (asin, slug, clicked_at) "
                    "VALUES (?, ?, ?)",
                    ('B00TEST', slug, f'{when} 12:00:00'),
                )
            conn.commit()
        finally:
            conn.close()

    def _seed_post(self, slug, creator_id, product_name='Test Product',
                   angle='angle-a', collection_slug=None, status='posted'):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO posts "
                "(creator_id, asin, slug, product_name, angle, "
                " collection_slug, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (creator_id, 'B00TEST', slug, product_name, angle,
                 collection_slug, status),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_earnings(self, asin, creator_id, earnings, units=1,
                       period_start=None, period_end=None):
        period_start = period_start or _INSIDE_WINDOW
        period_end = period_end or _INSIDE_WINDOW
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO earnings_amazon "
                "(creator_id, asin, product_name, period_start, period_end, "
                " earnings, units) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (creator_id, asin, f'Product {asin}', period_start,
                 period_end, earnings, units),
            )
            conn.commit()
        finally:
            conn.close()

    def _window(self):
        # 7d window matching the v2 default — anything dated _YESTERDAY is in.
        start = (_TODAY - timedelta(days=6)).isoformat()
        end = _TODAY.isoformat()
        return start, end


# ── 1. data-layer ranking ─────────────────────────────────────────────────────

class CollectionsRankedTests(_InsightsTestBase):

    def test_collections_ranked_orders_by_clicks_desc(self):
        self._seed_collage('coll-a', 'creator-1', hero_title='Coll A')
        self._seed_collage('coll-b', 'creator-1', hero_title='Coll B')
        self._seed_collage('coll-c', 'creator-1', hero_title='Coll C')
        self._seed_clicks('coll-a', 2)
        self._seed_clicks('coll-b', 7)
        self._seed_clicks('coll-c', 4)

        start, end = self._window()
        rows = self.insights.collections_ranked('creator-1', start, end)
        slugs_in_order = [r['slug'] for r in rows]
        self.assertEqual(slugs_in_order[:3], ['coll-b', 'coll-c', 'coll-a'])
        self.assertEqual(rows[0]['clicks'], 7)
        self.assertEqual(rows[1]['clicks'], 4)
        self.assertEqual(rows[2]['clicks'], 2)
        # Secondary metric ('views') is None until issue #87 lands.
        self.assertIsNone(rows[0]['views'])


class PostsRankedTests(_InsightsTestBase):

    def test_posts_ranked_orders_by_clicks_desc(self):
        self._seed_post('post-a', 'creator-1', product_name='A')
        self._seed_post('post-b', 'creator-1', product_name='B')
        self._seed_post('post-c', 'creator-1', product_name='C')
        self._seed_clicks('post-a', 1)
        self._seed_clicks('post-b', 5)
        self._seed_clicks('post-c', 3)

        start, end = self._window()
        rows = self.insights.posts_ranked('creator-1', start, end)
        slugs_in_order = [r['slug'] for r in rows]
        self.assertEqual(slugs_in_order[:3], ['post-b', 'post-c', 'post-a'])
        self.assertEqual(rows[0]['clicks'], 5)
        self.assertIsNone(rows[0]['views'])


class ProductsRankedTests(_InsightsTestBase):

    def test_products_ranked_orders_by_earnings_desc(self):
        self._seed_earnings('B00ASIN1', 'creator-1', earnings=12.50)
        self._seed_earnings('B00ASIN2', 'creator-1', earnings=99.00)
        self._seed_earnings('B00ASIN3', 'creator-1', earnings=44.25)

        start, end = self._window()
        rows = self.insights.products_ranked('creator-1', start, end)
        asins_in_order = [r['asin'] for r in rows]
        self.assertEqual(asins_in_order[:3], ['B00ASIN2', 'B00ASIN3', 'B00ASIN1'])
        self.assertEqual(rows[0]['earnings'], 99.00)
        self.assertIsNone(rows[0]['views'])


class RetailersRankedTests(_InsightsTestBase):

    def test_retailers_ranked_aggregates_amazon_earnings(self):
        self._seed_earnings('B001', 'creator-1', earnings=10.00, units=1)
        self._seed_earnings('B002', 'creator-1', earnings=25.50, units=2)
        self._seed_earnings('B003', 'creator-1', earnings=4.50, units=1)

        start, end = self._window()
        rows = self.insights.retailers_ranked('creator-1', start, end)
        # PR-1 scope: single Amazon row.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['retailer'], 'Amazon')
        self.assertEqual(rows[0]['earnings'], 40.00)
        self.assertEqual(rows[0]['units'], 4)
        self.assertIsNone(rows[0]['views'])


# ── 2. creator scoping ────────────────────────────────────────────────────────

class CreatorScopingTests(_InsightsTestBase):

    def test_helpers_scoped_to_creator_id(self):
        # Two creators, mirrored data — never cross-leak.
        self._seed_collage('coll-1', 'creator-1', hero_title='C1 Collection')
        self._seed_collage('coll-2', 'creator-2', hero_title='C2 Collection')
        self._seed_clicks('coll-1', 3)
        self._seed_clicks('coll-2', 8)

        self._seed_post('post-1', 'creator-1', product_name='C1 Post')
        self._seed_post('post-2', 'creator-2', product_name='C2 Post')
        self._seed_clicks('post-1', 2)
        self._seed_clicks('post-2', 6)

        self._seed_earnings('B0C1', 'creator-1', earnings=10.0)
        self._seed_earnings('B0C2', 'creator-2', earnings=99.0)

        start, end = self._window()

        c1_colls = self.insights.collections_ranked('creator-1', start, end)
        c2_colls = self.insights.collections_ranked('creator-2', start, end)
        self.assertEqual([r['slug'] for r in c1_colls], ['coll-1'])
        self.assertEqual([r['slug'] for r in c2_colls], ['coll-2'])

        c1_posts = self.insights.posts_ranked('creator-1', start, end)
        c2_posts = self.insights.posts_ranked('creator-2', start, end)
        self.assertEqual([r['slug'] for r in c1_posts], ['post-1'])
        self.assertEqual([r['slug'] for r in c2_posts], ['post-2'])

        c1_prods = self.insights.products_ranked('creator-1', start, end)
        c2_prods = self.insights.products_ranked('creator-2', start, end)
        self.assertEqual([r['asin'] for r in c1_prods], ['B0C1'])
        self.assertEqual([r['asin'] for r in c2_prods], ['B0C2'])

        c1_ret = self.insights.retailers_ranked('creator-1', start, end)
        c2_ret = self.insights.retailers_ranked('creator-2', start, end)
        self.assertEqual(c1_ret[0]['earnings'], 10.0)
        self.assertEqual(c2_ret[0]['earnings'], 99.0)


# ── 3. empty / missing data ───────────────────────────────────────────────────

class EmptyDataTests(_InsightsTestBase):

    def test_helpers_return_empty_list_when_no_data(self):
        start, end = self._window()
        self.assertEqual(
            self.insights.collections_ranked('nobody', start, end), [])
        self.assertEqual(
            self.insights.posts_ranked('nobody', start, end), [])
        self.assertEqual(
            self.insights.products_ranked('nobody', start, end), [])
        self.assertEqual(
            self.insights.retailers_ranked('nobody', start, end), [])


# ── 4. route flag gating ──────────────────────────────────────────────────────

class V2RouteRenderTests(_InsightsTestBase):

    def _client_authed(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess['admin_authed'] = True
        return client

    def test_v2_route_renders_when_flag_enabled(self):
        # Seed something for each section so empty-state strings don't
        # accidentally satisfy assertions.
        self._seed_collage('flag-coll', 'everydaywithsteph',
                           hero_title='Flag Collection')
        self._seed_clicks('flag-coll', 4)
        self._seed_post('flag-post', 'everydaywithsteph',
                        product_name='Flag Post')
        self._seed_clicks('flag-post', 2)
        self._seed_earnings('B0FLAG', 'everydaywithsteph', earnings=12.34)

        os.environ['INSIGHTS_V2_ENABLED'] = '1'
        try:
            client = self._client_authed()
            resp = client.get('/insights')
        finally:
            os.environ.pop('INSIGHTS_V2_ENABLED', None)

        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        # All 4 section headings present.
        self.assertIn('Best Collections by click traffic', body)
        self.assertIn('Best Posts by click traffic', body)
        self.assertIn('Best Products by earnings', body)
        self.assertIn('Best Retailers by earnings', body)
        # v2 template specifics — no legacy tabs.
        self.assertNotIn('class="tabs"', body)


class LegacyRouteRenderTests(_InsightsTestBase):

    def _client_authed(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess['admin_authed'] = True
        return client

    def test_legacy_route_unchanged_when_flag_disabled(self):
        os.environ.pop('INSIGHTS_V2_ENABLED', None)
        client = self._client_authed()
        resp = client.get('/insights')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        # Legacy template still ships its tabbed UI.
        self.assertIn('class="tabs"', body)
        # And does NOT show the v2 section headings.
        self.assertNotIn('Best Collections by click traffic', body)


# ── 5. default-window assertion ───────────────────────────────────────────────

class DefaultWindowTests(_InsightsTestBase):

    def _client_authed(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess['admin_authed'] = True
        return client

    def test_default_window_is_7d_in_v2_path(self):
        # We capture the kwargs handed to render_template by the v2 branch.
        os.environ['INSIGHTS_V2_ENABLED'] = '1'
        captured = {}

        def _spy(template_name, **ctx):
            captured['template'] = template_name
            captured['ctx'] = ctx
            return ''

        try:
            with mock.patch.object(self.app_module, 'render_template', _spy):
                client = self._client_authed()
                client.get('/insights')
        finally:
            os.environ.pop('INSIGHTS_V2_ENABLED', None)

        self.assertEqual(captured.get('template'), 'insights_v2.html')
        self.assertEqual(captured['ctx'].get('window'), '7d')
        # Sanity: the resolved start/end should be a 7-day span.
        start = captured['ctx']['start']
        end = captured['ctx']['end']
        self.assertEqual(end, _TODAY.isoformat())
        self.assertEqual(start, (_TODAY - timedelta(days=6)).isoformat())


if __name__ == '__main__':
    unittest.main()
