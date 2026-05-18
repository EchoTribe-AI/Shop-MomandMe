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


class BestListShapingTests(_InsightsTestBase):
    """Patch-2 audit follow-ups: a 'Best' list must exclude 0-click rows
    and stay capped. Pinning both behaviors so later refactors can't
    silently regress to LEFT JOIN / unbounded result sets."""

    def test_collections_ranked_excludes_zero_click_rows(self):
        self._seed_collage('coll-clicked', 'creator-1', hero_title='Clicked')
        self._seed_collage('coll-silent', 'creator-1', hero_title='Silent')
        self._seed_clicks('coll-clicked', 3)
        # 'coll-silent' deliberately gets no clicks.

        start, end = self._window()
        rows = self.insights.collections_ranked('creator-1', start, end)
        slugs = [r['slug'] for r in rows]
        self.assertEqual(slugs, ['coll-clicked'])
        self.assertNotIn('coll-silent', slugs)

    def test_posts_ranked_excludes_zero_click_rows(self):
        self._seed_post('post-clicked', 'creator-1', product_name='Clicked')
        self._seed_post('post-silent', 'creator-1', product_name='Silent')
        self._seed_clicks('post-clicked', 4)
        # 'post-silent' deliberately gets no clicks.

        start, end = self._window()
        rows = self.insights.posts_ranked('creator-1', start, end)
        slugs = [r['slug'] for r in rows]
        self.assertEqual(slugs, ['post-clicked'])
        self.assertNotIn('post-silent', slugs)

    def test_collections_ranked_capped_at_20(self):
        # 25 collections, each with a distinct (descending) click count so
        # the LIMIT can't accidentally pass by hitting an ordering tie.
        for i in range(25):
            slug = f'coll-{i:02d}'
            self._seed_collage(slug, 'creator-1', hero_title=f'Coll {i:02d}')
            self._seed_clicks(slug, 25 - i)  # 25 clicks down to 1

        start, end = self._window()
        rows = self.insights.collections_ranked('creator-1', start, end)
        self.assertEqual(len(rows), 20)
        # Top 20 must be the highest-click ones in clicks-desc order.
        expected_top_slugs = [f'coll-{i:02d}' for i in range(20)]
        self.assertEqual([r['slug'] for r in rows], expected_top_slugs)

    def test_collections_ranked_returns_empty_when_no_clicks(self):
        # Empty-state path still works: collections exist, but none clicked.
        for i in range(5):
            self._seed_collage(f'coll-{i}', 'creator-1',
                               hero_title=f'Coll {i}')
        start, end = self._window()
        rows = self.insights.collections_ranked('creator-1', start, end)
        self.assertEqual(rows, [])


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


# ── 6. _table_exists() backend awareness ──────────────────────────────────────
# Regression: an earlier implementation probed sqlite_master first on every
# connection. On a real Postgres connection that errors and (worse) leaves
# psycopg2's transaction in an aborted state, so every subsequent query on
# the same connection fails too — production v2 insights came back empty.
# These tests pin the branch behavior with a fake connection so the suite can
# verify both paths without standing up a real Postgres.

class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Records the last SQL string executed and returns a canned row.

    Use `raise_on_pg=True` to simulate the prior aborted-transaction bug:
    the SQLite-style probe raises, mirroring what psycopg2 does when given
    `SELECT 1 FROM sqlite_master ...`.
    """

    def __init__(self, return_row=(1,), raise_on_sqlite_master=False):
        self.return_row = return_row
        self.raise_on_sqlite_master = raise_on_sqlite_master
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if self.raise_on_sqlite_master and 'sqlite_master' in sql:
            raise RuntimeError(
                'simulated psycopg2 ProgrammingError: relation "sqlite_master" '
                'does not exist'
            )
        return _FakeCursor(self.return_row)


class TableExistsBackendAwareTests(unittest.TestCase):
    """Patch db_schema._USE_PG and assert the right SQL flavor runs."""

    def test_pg_branch_queries_information_schema_not_sqlite_master(self):
        import insights as ins
        fake = _FakeConn(return_row=(1,), raise_on_sqlite_master=True)
        with mock.patch('db_schema._USE_PG', True):
            result = ins._table_exists(fake, 'click_log')
        self.assertTrue(result)
        # Must never touch sqlite_master on the PG branch — that was the
        # bug that aborted the connection's transaction.
        joined = ' | '.join(fake.executed)
        self.assertNotIn('sqlite_master', joined)
        self.assertIn('information_schema.tables', joined)

    def test_sqlite_branch_queries_sqlite_master(self):
        import insights as ins
        fake = _FakeConn(return_row=(1,))
        with mock.patch('db_schema._USE_PG', False):
            result = ins._table_exists(fake, 'click_log')
        self.assertTrue(result)
        joined = ' | '.join(fake.executed)
        self.assertIn('sqlite_master', joined)
        self.assertNotIn('information_schema', joined)

    def test_pg_branch_returns_false_on_missing_table(self):
        import insights as ins
        fake = _FakeConn(return_row=None)
        with mock.patch('db_schema._USE_PG', True):
            self.assertFalse(ins._table_exists(fake, 'no_such_table'))

    def test_sqlite_branch_returns_false_on_missing_table(self):
        import insights as ins
        fake = _FakeConn(return_row=None)
        with mock.patch('db_schema._USE_PG', False):
            self.assertFalse(ins._table_exists(fake, 'no_such_table'))


# ── 7. daily_traffic data helper (PR 2) ───────────────────────────────────────

class DailyTrafficTests(_InsightsTestBase):

    def _seed_click_on_date(self, slug, when_iso):
        """Insert one click_log row dated YYYY-MM-DD HH:MM:SS."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO click_log (asin, slug, clicked_at) VALUES (?, ?, ?)",
                ('B00TEST', slug, f'{when_iso} 12:00:00'),
            )
            conn.commit()
        finally:
            conn.close()

    def test_daily_traffic_groups_clicks_by_day(self):
        # Two clicks on day-1, three on day-3, none on day-2; ensure each
        # bucket carries the right count.
        self._seed_collage('coll-a', 'creator-1')
        d1 = (_TODAY - timedelta(days=4)).isoformat()
        d2 = (_TODAY - timedelta(days=3)).isoformat()
        d3 = (_TODAY - timedelta(days=2)).isoformat()
        self._seed_click_on_date('coll-a', d1)
        self._seed_click_on_date('coll-a', d1)
        self._seed_click_on_date('coll-a', d3)
        self._seed_click_on_date('coll-a', d3)
        self._seed_click_on_date('coll-a', d3)

        start, end = self._window()  # 7d window — all three days in
        rows = self.insights.daily_traffic('creator-1', start, end)
        by_date = {r['date']: r['clicks'] for r in rows}
        self.assertEqual(by_date.get(d1), 2)
        self.assertEqual(by_date.get(d2), 0)
        self.assertEqual(by_date.get(d3), 3)

    def test_daily_traffic_zero_fills_missing_dates(self):
        # No clicks at all — every day in the 7d window must appear with 0.
        self._seed_collage('silent-coll', 'creator-1')
        start, end = self._window()
        rows = self.insights.daily_traffic('creator-1', start, end)
        self.assertEqual(len(rows), 7)
        # Sorted ascending.
        dates = [r['date'] for r in rows]
        self.assertEqual(dates, sorted(dates))
        self.assertTrue(all(r['clicks'] == 0 for r in rows))

    def test_daily_traffic_scoped_to_creator_id(self):
        # Same window, two creators each with their own collection and
        # one click — neither should leak into the other's totals.
        self._seed_collage('coll-c1', 'creator-1')
        self._seed_collage('coll-c2', 'creator-2')
        d1 = (_TODAY - timedelta(days=2)).isoformat()
        self._seed_click_on_date('coll-c1', d1)
        self._seed_click_on_date('coll-c2', d1)
        self._seed_click_on_date('coll-c2', d1)

        start, end = self._window()
        c1_total = sum(r['clicks'] for r in
                       self.insights.daily_traffic('creator-1', start, end))
        c2_total = sum(r['clicks'] for r in
                       self.insights.daily_traffic('creator-2', start, end))
        self.assertEqual(c1_total, 1)
        self.assertEqual(c2_total, 2)

    def test_daily_traffic_avoids_double_counting_slug_overlap(self):
        """The data-layer guarantee Kelly flagged: a slug that exists in
        BOTH collages and posts must not double-count its clicks in the
        daily-traffic total. PR 1's Best Collections / Best Posts helpers
        intentionally allow the double-count (each section sums its own
        join); daily_traffic uses COUNT(DISTINCT click_log.id) to render
        a single honest line for the chart.
        """
        # Seed the same slug as BOTH a collection AND a post for the
        # same creator. Then drop 4 clicks on that slug.
        shared_slug = 'shared-slug'
        self._seed_collage(shared_slug, 'creator-1', hero_title='Shared')
        self._seed_post(shared_slug, 'creator-1', product_name='Shared Post')
        d1 = (_TODAY - timedelta(days=2)).isoformat()
        for _ in range(4):
            self._seed_click_on_date(shared_slug, d1)

        start, end = self._window()
        rows = self.insights.daily_traffic('creator-1', start, end)
        by_date = {r['date']: r['clicks'] for r in rows}
        # 4 clicks total — NOT 8. UNION + COUNT(DISTINCT click_log.id)
        # keeps each click counted once even though the slug subquery
        # surfaces it via both collages and posts branches.
        self.assertEqual(by_date.get(d1), 4)


# ── 8. apply_indicators threshold logic ───────────────────────────────────────

class ApplyIndicatorsTests(unittest.TestCase):

    def setUp(self):
        # Pure-Python helper — no DB needed.
        import insights as ins
        self.ins = ins

    def _rows(self, n):
        return [{'slug': f's{i}', 'clicks': n - i} for i in range(n)]

    def test_indicators_flame_for_top_3_when_section_has_6_or_more(self):
        out = self.ins.apply_indicators(self._rows(10))
        self.assertEqual([r['indicator'] for r in out[:3]],
                         ['flame', 'flame', 'flame'])

    def test_indicators_red_dot_for_bottom_3_when_section_has_6_or_more(self):
        out = self.ins.apply_indicators(self._rows(10))
        self.assertEqual([r['indicator'] for r in out[-3:]],
                         ['red_dot', 'red_dot', 'red_dot'])
        # Middle rows must carry no indicator at all.
        self.assertTrue(all(r['indicator'] is None for r in out[3:7]))

    def test_indicators_flame_only_for_section_with_5_rows(self):
        # Boundary: 5 rows is below the _RED_DOT_MIN_ROWS = 6 cutoff,
        # so we get top-1 = flame and no red_dot anywhere.
        out = self.ins.apply_indicators(self._rows(5))
        self.assertEqual(out[0]['indicator'], 'flame')
        self.assertTrue(all(r['indicator'] is None for r in out[1:]))
        # Specifically: no red_dot at all on a 5-row section.
        self.assertNotIn('red_dot', [r['indicator'] for r in out])

    def test_indicators_flame_only_for_section_with_1_row(self):
        # The single-row retailer case (Amazon today, pre-Walmart).
        out = self.ins.apply_indicators(self._rows(1))
        self.assertEqual(out[0]['indicator'], 'flame')

    def test_indicators_empty_section_yields_no_indicators(self):
        self.assertEqual(self.ins.apply_indicators([]), [])

    def test_indicators_does_not_mutate_input(self):
        # apply_indicators must return new dicts, not mutate the rows
        # the ranking helpers handed in.
        rows = self._rows(8)
        snapshot = [dict(r) for r in rows]
        _ = self.ins.apply_indicators(rows)
        self.assertEqual(rows, snapshot)
        self.assertFalse(any('indicator' in r for r in rows))


# ── 9. rendered HTML — chart SVG + indicator emoji ────────────────────────────

class RenderedHtmlV2Tests(_InsightsTestBase):

    def _client_authed(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess['admin_authed'] = True
        return client

    def _seed_click_on_date(self, slug, when_iso):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO click_log (asin, slug, clicked_at) VALUES (?, ?, ?)",
                ('B00TEST', slug, f'{when_iso} 12:00:00'),
            )
            conn.commit()
        finally:
            conn.close()

    def test_rendered_html_contains_chart_svg(self):
        # Seed enough traffic that the chart has a non-empty polyline.
        self._seed_collage('flag-coll', 'everydaywithsteph',
                           hero_title='Flag Collection')
        d1 = (_TODAY - timedelta(days=2)).isoformat()
        for _ in range(3):
            self._seed_click_on_date('flag-coll', d1)

        os.environ['INSIGHTS_V2_ENABLED'] = '1'
        try:
            resp = self._client_authed().get('/insights')
        finally:
            os.environ.pop('INSIGHTS_V2_ENABLED', None)

        body = resp.data.decode('utf-8')
        self.assertIn('iv-chart__svg', body)
        self.assertIn('<polyline', body)
        self.assertIn('iv-chart__line', body)

    def test_rendered_html_contains_flame_for_top_collection(self):
        # Top collection by clicks should pick up the flame emoji.
        self._seed_collage('top-coll', 'everydaywithsteph', hero_title='Top')
        d1 = (_TODAY - timedelta(days=2)).isoformat()
        for _ in range(5):
            self._seed_click_on_date('top-coll', d1)

        os.environ['INSIGHTS_V2_ENABLED'] = '1'
        try:
            resp = self._client_authed().get('/insights')
        finally:
            os.environ.pop('INSIGHTS_V2_ENABLED', None)

        body = resp.data.decode('utf-8')
        # 🔥 must appear at least once in the body for the top row.
        self.assertIn('🔥', body)
        # And the slug it belongs to must be present in the same response.
        self.assertIn('top-coll', body)

    def test_chart_empty_state_when_all_zero_clicks(self):
        # Codex refinement: when daily_traffic has zero clicks every day,
        # render the empty-state copy — NOT a flat zero-polyline.
        self._seed_collage('silent-coll', 'everydaywithsteph',
                           hero_title='Silent Collection')
        # No clicks seeded — every day in the window is zero.

        os.environ['INSIGHTS_V2_ENABLED'] = '1'
        try:
            resp = self._client_authed().get('/insights')
        finally:
            os.environ.pop('INSIGHTS_V2_ENABLED', None)

        body = resp.data.decode('utf-8')
        # Empty-state copy is present.
        self.assertIn('No traffic in this window', body)
        # The polyline element is NOT present — we don't render a flat line.
        self.assertNotIn('<polyline', body)


if __name__ == '__main__':
    unittest.main()
