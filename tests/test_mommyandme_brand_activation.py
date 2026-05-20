"""Mommy & Me brand activation — downstream-only coverage.

Exercises the production-like end-to-end render path for the Sage Forward
palette confirmed by Steph 2026-05-18. Unlike the shared upstream coverage
in tests/test_storefront_framework_boundary.py — which monkeypatches
app._BRANDING_DIR to an isolated tmp directory — these tests run against
the REAL branding/ directory committed in this PR. That is the whole
point: prove that the on-disk overrides.json + logo.png reach the rendered
storefront response without any test scaffolding masking a misconfiguration.

DB is still isolated to a tmp SQLite path so we can seed the published
collage the end-to-end render needs without touching prod state.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock


# Sage Forward palette — confirmed by Steph 2026-05-18.
# Mirrors branding/overrides.json verbatim. Any drift between this tuple
# and the on-disk JSON should fail test_overrides_json_values_loaded.
SAGE_FORWARD = {
    'shop_name':                  'The Mommy & Me Collective',
    'shop_domain':                'shop.mommyandmecollective.com',
    'brand_primary':              '#7C7D6A',
    'brand_on_primary':           '#F5F2ED',
    'brand_primary_container':    '#DDBBA4',
    'brand_on_primary_container': '#3D3A33',
    'brand_surface':              '#E5DBC8',
    'brand_on_surface':           '#3D3A33',
}


class _ActivationTestBase(unittest.TestCase):
    """Isolated DB + real on-disk branding/ + brand cache reset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'mmc-activation.db')
        os.environ['CACHE_DB_PATH'] = self.db_path
        os.environ.pop('ACTIVE_CREATOR_ID', None)

        import db_schema
        import app
        self.db_schema = db_schema
        self.app_module = app

        db_schema.DB_PATH = self.db_path
        db_schema.bootstrap()

        # Critical: do NOT monkeypatch _BRANDING_DIR. We want the real
        # repo-root branding/ to drive the loader. Reset the cache so any
        # state from earlier tests in the run (which DO monkeypatch the
        # dir) does not leak in.
        self.app_module._branding_cache_reset()

    def tearDown(self):
        self.app_module._branding_cache_reset()
        self.tmp.cleanup()

    def _branding_dir(self):
        return os.path.join(
            os.path.dirname(os.path.abspath(self.app_module.__file__)),
            'branding',
        )

    def _seed_collage(self, slug, creator_id='everydaywithsteph'):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO collages
                (slug, products_json, caption, creator_id, status, hero_title, click_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (slug, json.dumps([]), 'caption', creator_id, 'published',
                 'Mommy & Me Test', 0),
            )
            conn.commit()
        finally:
            conn.close()


class OverridesJsonLoaded(_ActivationTestBase):
    """branding/overrides.json values reach the loader output."""

    def test_overrides_json_values_loaded(self):
        overrides = self.app_module._load_branding_overrides()
        for key, expected in SAGE_FORWARD.items():
            self.assertEqual(
                overrides.get(key), expected,
                f'overrides.json key {key!r} did not load with expected value',
            )

    def test_overrides_json_does_not_contain_logo_url_key(self):
        # Loader derives logo_url from disk, not from overrides.json. The
        # JSON file must NOT include logo_url or the on-disk path would
        # be silently clobbered by the overrides.update(data) line.
        path = os.path.join(self._branding_dir(), 'overrides.json')
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertNotIn(
            'logo_url', data,
            'branding/overrides.json must not include logo_url — let the '
            'loader derive it from branding/logo.png on disk.',
        )


class LogoUrlDerivedFromDisk(_ActivationTestBase):
    """logo_url is derived from the real branding/logo.png, not JSON."""

    def test_logo_url_points_at_branding_logo_png(self):
        overrides = self.app_module._load_branding_overrides()
        self.assertEqual(overrides.get('logo_url'), '/branding/logo.png')

    def test_branding_logo_png_exists_on_disk(self):
        # Sanity guard: if someone removes the logo, the test that follows
        # would silently fall back to a clean 404 and we'd lose coverage.
        logo_path = os.path.join(self._branding_dir(), 'logo.png')
        self.assertTrue(
            os.path.isfile(logo_path),
            f'branding/logo.png missing from repo at {logo_path}',
        )
        self.assertGreater(
            os.path.getsize(logo_path), 0,
            'branding/logo.png exists but is empty',
        )


class BrandingAssetRoutes(_ActivationTestBase):
    """Public /branding/ route serves the logo and blocks overrides.json."""

    def test_branding_logo_route_returns_200_with_body(self):
        client = self.app_module.app.test_client()
        resp = client.get('/branding/logo.png')
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(
            len(resp.data), 0,
            '/branding/logo.png returned 200 but empty body',
        )

    def test_branding_overrides_json_route_returns_404(self):
        client = self.app_module.app.test_client()
        resp = client.get('/branding/overrides.json')
        self.assertEqual(
            resp.status_code, 404,
            'overrides.json must be blocked by the asset extension whitelist',
        )

    def test_branding_overrides_json_does_not_leak_hex_values(self):
        # Belt-and-braces: even if the response body were non-empty for
        # some unforeseen reason, none of the brand hex values must
        # appear in it.
        client = self.app_module.app.test_client()
        resp = client.get('/branding/overrides.json')
        body = resp.data
        for hex_value in (
            b'#7C7D6A', b'#7c7d6a',
            b'#E5DBC8', b'#e5dbc8',
            b'#DDBBA4', b'#ddbba4',
        ):
            self.assertNotIn(
                hex_value, body,
                f'/branding/overrides.json response leaked {hex_value!r}',
            )


class BuildBrandContextReturnsSageForward(_ActivationTestBase):
    """build_brand_context picks up the overrides.json values."""

    def test_build_brand_context_for_everydaywithsteph(self):
        ctx = self.app_module.build_brand_context('everydaywithsteph')
        for key, expected in SAGE_FORWARD.items():
            self.assertEqual(
                ctx.get(key), expected,
                f'build_brand_context returned wrong value for {key}: '
                f'got {ctx.get(key)!r}, expected {expected!r}',
            )
        # logo_url derived from disk also reaches the context.
        self.assertEqual(ctx.get('logo_url'), '/branding/logo.png')


class EndToEndStorefrontRender(_ActivationTestBase):
    """The full chain: overrides.json + logo.png → rendered HTML."""

    def test_storefront_response_contains_sage_and_linen_hex(self):
        # Seed a published collage under the demo creator id. The host
        # match is irrelevant here — overrides.json is the highest-
        # precedence layer, so values land regardless of which creator
        # the request resolves to.
        self._seed_collage(slug='mommyandme-activation-test')

        client = self.app_module.app.test_client()
        resp = client.get('/shop/mommyandme-activation-test')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8').lower()
        # _brand_vars.html renders the dict value verbatim; the Sage
        # Forward palette uses uppercase hex but Jinja preserves case.
        # Lowercase the body and compare both forms to stay robust.
        self.assertIn(
            '#7c7d6a', body,
            'brand_primary (sage) hex must appear in rendered storefront',
        )
        self.assertIn(
            '#e5dbc8', body,
            'brand_surface (linen) hex must appear in rendered storefront',
        )
        # Bridge proof: --bg mirrored from brand_surface, --text from
        # brand_on_surface. Both must be present so legacy templates
        # repaint to the new canvas/text.
        self.assertIn('--bg: #e5dbc8', body)
        self.assertIn('--text: #3d3a33', body)


class PublicNavUsesRelativeUrls(_ActivationTestBase):
    """Public storefront nav must use relative paths only.

    Before the patch, _public_shop_nav() built absolute hrefs prefixed
    with https://{SHOP_SUBDOMAIN}/, which cross-domain-jumped every
    Mommy & Me shopper back to shop.echotribe.ai. Relative paths work
    on whichever host the framework is deployed under and remove the
    SHOP_SUBDOMAIN coupling for the public nav surface entirely.
    """

    def test_storefront_nav_uses_relative_urls(self):
        self._seed_collage(slug='nav-relative-test')

        client = self.app_module.app.test_client()
        resp = client.get('/shop/nav-relative-test')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')

        # Each nav link present as a literal relative href.
        # Plan §7: 'Social Posts' is hidden from the public nav (routes
        # /shop/posts, /posts, /admin/posts remain intact — only the visible
        # nav item is dropped). Add back here when posts ships.
        self.assertIn('href="/collections"', body)
        self.assertIn('href="/trends"', body)

        # Isolate the public nav block and assert no cross-domain hrefs
        # leaked back in. We narrow to the nav specifically because
        # canonical / OG / share / sitemap URLs intentionally remain
        # absolute (cross-domain consumers need a fully-qualified host);
        # those are tracked as a separate follow-up PR.
        nav_start = body.find('<nav class="public-shop-nav"')
        self.assertNotEqual(
            nav_start, -1,
            'public-shop-nav element missing from rendered response',
        )
        nav_end = body.find('</nav>', nav_start)
        self.assertNotEqual(nav_end, -1, 'public-shop-nav not closed')
        nav_block = body[nav_start:nav_end]
        self.assertNotIn(
            'shop.echotribe.ai', nav_block,
            'Public nav must not contain absolute shop.echotribe.ai hrefs',
        )
        self.assertNotIn(
            'https://', nav_block,
            'Public nav must use relative paths only — no absolute hrefs '
            'of any host',
        )


class StorefrontHeaderLogoRendering(_ActivationTestBase):
    """Header logo renders when brand.logo_url is set."""

    def test_storefront_header_renders_logo_when_brand_logo_url_set(self):
        self._seed_collage(slug='logo-render-test')

        client = self.app_module.app.test_client()
        resp = client.get('/shop/logo-render-test')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')

        # The partial renders a single <img> with the on-disk logo path.
        # Match on the src + alt text together to avoid false hits from
        # any unrelated <img> on the page.
        self.assertIn('src="/branding/logo.png"', body)
        self.assertIn(
            'alt="The Mommy &amp; Me Collective"', body,
            'Logo <img> must use brand.shop_name (HTML-escaped) for alt text',
        )


class StorefrontHeaderFallsBackToTextWhenLogoAbsent(unittest.TestCase):
    """Logo partial is inert when brand.logo_url is falsy.

    Uses a tmp branding/ dir with overrides.json but NO logo.png so
    _load_branding_overrides() returns brand colors/shop_name but
    leaves logo_url absent. The end-to-end render must then omit the
    <img> entirely while still painting the text-only header.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'mmc-fallback.db')
        os.environ['CACHE_DB_PATH'] = self.db_path
        os.environ.pop('ACTIVE_CREATOR_ID', None)

        import db_schema
        import app
        self.db_schema = db_schema
        self.app_module = app

        db_schema.DB_PATH = self.db_path
        db_schema.bootstrap()

        # Tmp branding/ with overrides.json + NO logo.png.
        self.branding_dir = tempfile.mkdtemp(prefix='mmc-fallback-branding-')
        overrides_path = os.path.join(self.branding_dir, 'overrides.json')
        with open(overrides_path, 'w', encoding='utf-8') as f:
            json.dump({
                'shop_name':     'The Mommy & Me Collective',
                'shop_domain':   'shop.mommyandmecollective.com',
                'brand_primary': '#7C7D6A',
                'brand_surface': '#E5DBC8',
            }, f)

        self._branding_patch = mock.patch.object(
            app, '_BRANDING_DIR', self.branding_dir,
        )
        self._branding_patch.start()
        app._branding_cache_reset()

    def tearDown(self):
        self._branding_patch.stop()
        self.app_module._branding_cache_reset()
        try:
            for entry in os.listdir(self.branding_dir):
                os.remove(os.path.join(self.branding_dir, entry))
            os.rmdir(self.branding_dir)
        except FileNotFoundError:
            pass
        self.tmp.cleanup()

    def _seed_collage(self, slug):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO collages
                (slug, products_json, caption, creator_id, status, hero_title, click_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (slug, json.dumps([]), 'caption', 'everydaywithsteph',
                 'published', 'Fallback Test', 0),
            )
            conn.commit()
        finally:
            conn.close()

    def test_storefront_header_falls_back_to_text_when_brand_logo_url_absent(self):
        # Sanity: the loader sees no logo_url.
        overrides = self.app_module._load_branding_overrides()
        self.assertNotIn(
            'logo_url', overrides,
            'Tmp branding/ has no logo.png — loader must not derive a '
            'logo_url for this fallback test to be meaningful',
        )

        self._seed_collage(slug='logo-fallback-test')

        client = self.app_module.app.test_client()
        resp = client.get('/shop/logo-fallback-test')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')

        # No <img> for the brand logo emitted.
        self.assertNotIn(
            'src="/branding/logo.png"', body,
            'Logo <img> must not render when brand.logo_url is absent',
        )
        # And the existing text-header is still present (the .brand div
        # is the anchor element rendered by shop_landing.html — it does
        # not depend on the logo partial).
        self.assertIn(
            'class="brand"', body,
            'Text header (.brand) must still render when logo is absent',
        )


class UniversalBottomNav(unittest.TestCase):
    """5-item bottom nav rendered by templates/partials/_mobile_chrome.html.

    Pinned to the canonical app routes:
        Home    → /hub
        Create  → /walmart/trending-now?admin=1
        Manage  → /admin/posts
        Chat    → /chat
        Insights → /insights

    Any change to nav order, route hrefs, or the 5-item count must update
    this test alongside _mobile_chrome.html.
    """

    EXPECTED_NAV = (
        ('home',     '/hub',                          'Home'),
        ('create',   '/walmart/trending-now?admin=1', 'Create'),
        ('manage',   '/admin/posts',                  'Manage'),
        ('chat',     '/chat',                         'Chat'),
        ('insights', '/insights',                     'Insights'),
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'chrome-nav.db')
        os.environ['CACHE_DB_PATH'] = self.db_path
        os.environ.pop('ACTIVE_CREATOR_ID', None)

        import db_schema
        import app
        self.db_schema = db_schema
        self.app_module = app
        db_schema.DB_PATH = self.db_path
        db_schema.bootstrap()

        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def tearDown(self):
        self.tmp.cleanup()

    def _chrome_nav_block(self, body):
        """Return the <nav class="mc-bottom-nav"...> ... </nav> substring."""
        start = body.find('<nav class="mc-bottom-nav"')
        self.assertNotEqual(
            start, -1, 'mc-bottom-nav element missing from response',
        )
        end = body.find('</nav>', start)
        self.assertNotEqual(end, -1, 'mc-bottom-nav not closed')
        return body[start:end + len('</nav>')]

    def test_chrome_renders_exactly_five_nav_items(self):
        # /chat is the cheapest endpoint that uses the chrome partial.
        resp = self.client.get('/chat')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        nav_block = self._chrome_nav_block(body)

        # Count anchor tags inside the nav block.
        anchor_count = nav_block.count('<a ')
        self.assertEqual(
            anchor_count, 5,
            f'Bottom nav must have exactly 5 items; rendered {anchor_count}.',
        )

    def test_chrome_nav_hrefs_and_labels_match_canonical_routes(self):
        resp = self.client.get('/chat')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        nav_block = self._chrome_nav_block(body)

        for key, href, label in self.EXPECTED_NAV:
            self.assertIn(
                f'href="{href}"', nav_block,
                f'Nav item {key!r} must link to {href!r}',
            )
            self.assertIn(
                f'>{label}<', nav_block,
                f'Nav item {key!r} must show label {label!r}',
            )

    def test_chrome_nav_renders_in_order(self):
        # Order matters for visual rhythm; verify hrefs appear left-to-right
        # in the expected sequence.
        resp = self.client.get('/chat')
        self.assertEqual(resp.status_code, 200)
        nav_block = self._chrome_nav_block(resp.data.decode('utf-8'))

        last_pos = -1
        for _, href, _ in self.EXPECTED_NAV:
            pos = nav_block.find(f'href="{href}"')
            self.assertNotEqual(pos, -1, f'{href} missing from nav block')
            self.assertGreater(
                pos, last_pos,
                f'{href} should appear after the previous nav item',
            )
            last_pos = pos


class ChatPlaceholderRoute(unittest.TestCase):
    """/chat placeholder route — admin-gated, returns coming-soon text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'chat-placeholder.db')
        os.environ['CACHE_DB_PATH'] = self.db_path

        import db_schema
        import app
        self.db_schema = db_schema
        self.app_module = app
        db_schema.DB_PATH = self.db_path
        db_schema.bootstrap()

    def tearDown(self):
        self.tmp.cleanup()

    def _authed_client(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess['admin_authed'] = True
        return client

    def test_chat_returns_200_with_placeholder_text_when_authenticated(self):
        client = self._authed_client()
        resp = client.get('/chat')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        self.assertIn('Coming soon', body)
        self.assertIn('EchoAgent chat lands in Phase 2.5', body)

    def test_chat_requires_admin_auth(self):
        # No admin session → _require_admin_page redirects to /admin/login.
        # Pin that the placeholder is NOT publicly reachable.
        client = self.app_module.app.test_client()
        resp = client.get('/chat')
        self.assertNotEqual(
            resp.status_code, 200,
            '/chat must require admin auth; unauthenticated GET must not 200',
        )

    def test_chat_active_tab_aria_current(self):
        # Chat nav item should be marked aria-current="page" on /chat.
        client = self._authed_client()
        resp = client.get('/chat')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        # Find the chat anchor and confirm aria-current is on it specifically.
        chat_anchor_start = body.find('href="/chat"')
        self.assertNotEqual(chat_anchor_start, -1)
        # Look backward from the href to the opening <a tag for that anchor.
        a_open = body.rfind('<a ', 0, chat_anchor_start)
        a_close = body.find('>', chat_anchor_start)
        chat_anchor_tag = body[a_open:a_close + 1]
        self.assertIn('aria-current="page"', chat_anchor_tag)


class InsightsRouteStillRendersExistingPage(unittest.TestCase):
    """Guard against /insights being replaced with a coming-soon placeholder.

    /insights existed before the universal-nav PR; nav-wiring work must
    leave the real page intact. Asserts a 200 response and the absence
    of the chat placeholder copy on the body.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'insights-guard.db')
        os.environ['CACHE_DB_PATH'] = self.db_path

        import db_schema
        import app
        self.db_schema = db_schema
        self.app_module = app
        db_schema.DB_PATH = self.db_path
        db_schema.bootstrap()

    def tearDown(self):
        self.tmp.cleanup()

    def test_insights_returns_200_as_existing_page_not_placeholder(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as sess:
            sess['admin_authed'] = True
        resp = client.get('/insights')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        # The chat-placeholder copy must NOT appear on /insights — that
        # would mean someone wired /insights to the wrong handler.
        self.assertNotIn(
            'EchoAgent chat lands in Phase 2.5', body,
            '/insights response carries chat-placeholder text — the real '
            'insights page has been clobbered.',
        )


if __name__ == '__main__':
    unittest.main()
