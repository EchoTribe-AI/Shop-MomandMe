"""P0.7 storefront framework boundary tests.

Covers the runtime render path added in feature/p07-storefront-framework-boundary:
    1. _resolve_active_creator_id  — env > host/shop_domain > default
    2. _load_branding_overrides    — missing / partial / malformed branding/
    3. build_brand_context         — overrides > active row > demo row > defaults
    4. context_processor           — `brand` reaches rendered templates
    5. _brand_vars.html CSS vars   — non-NULL hex vs. NULL fallback
    6. /branding/<path:filename>   — serves present asset, 404s missing one
    7. Missing branding/           — no 500s on storefront routes

These tests use an isolated tmp SQLite DB and monkeypatch app._BRANDING_DIR
to a temp directory so the production repo layout doesn't bleed in.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock


class _BoundaryTestBase(unittest.TestCase):
    """Shared fixture: isolated DB + neutral branding/ dir + brand cache reset."""

    def setUp(self):
        # Isolated DB so creator-row mutations don't affect other tests.
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'p07.db')
        os.environ['CACHE_DB_PATH'] = self.db_path
        os.environ.pop('ACTIVE_CREATOR_ID', None)

        import db_schema
        import app
        self.db_schema = db_schema
        self.app_module = app

        db_schema.DB_PATH = self.db_path
        db_schema.bootstrap()

        # Isolated branding/ dir per test. Empty by default; tests that
        # exercise the loader populate it explicitly.
        self.branding_dir = tempfile.mkdtemp(prefix='p07-branding-')
        self._branding_patch = mock.patch.object(
            app, '_BRANDING_DIR', self.branding_dir
        )
        self._branding_patch.start()
        app._branding_cache_reset()

    def tearDown(self):
        self._branding_patch.stop()
        self.app_module._branding_cache_reset()
        self.tmp.cleanup()
        # branding_dir is a flat temp dir; remove its contents + the dir.
        try:
            for entry in os.listdir(self.branding_dir):
                os.remove(os.path.join(self.branding_dir, entry))
            os.rmdir(self.branding_dir)
        except FileNotFoundError:
            pass

    def _insert_creator(self, **fields):
        """Helper: insert a creator row with the given column overrides."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = {
                'id': fields.pop('id'),
                'display_name': fields.pop('display_name', 'Test Creator'),
            }
            # Allow the test to set any creators-table column.
            row.update(fields)
            cols = list(row.keys())
            placeholders = ', '.join('?' for _ in cols)
            conn.execute(
                f"INSERT INTO creators ({', '.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
            conn.commit()
        finally:
            conn.close()

    def _set_branding_dir_missing(self):
        """Point _BRANDING_DIR at a path that does not exist."""
        self._branding_patch.stop()
        ghost_path = os.path.join(self.tmp.name, 'no-such-branding-dir')
        self._branding_patch = mock.patch.object(
            self.app_module, '_BRANDING_DIR', ghost_path
        )
        self._branding_patch.start()
        self.app_module._branding_cache_reset()


# ── 1. resolver precedence ────────────────────────────────────────────────────

class ResolverPrecedence(_BoundaryTestBase):

    def test_env_var_wins(self):
        os.environ['ACTIVE_CREATOR_ID'] = 'env-creator'
        try:
            with self.app_module.app.test_request_context(
                '/', base_url='http://shop.example.com'
            ):
                self.assertEqual(
                    self.app_module._resolve_active_creator_id(),
                    'env-creator',
                )
        finally:
            os.environ.pop('ACTIVE_CREATOR_ID', None)

    def test_host_matches_shop_domain(self):
        # Distinct domain so we don't collide with DEFAULT_CREATOR's seeded
        # shop_domain (which points at the Mommy & Me production host).
        self._insert_creator(
            id='c1', shop_domain='shop.test-creator.example.com',
        )
        with self.app_module.app.test_request_context(
            '/', base_url='http://shop.test-creator.example.com',
        ):
            self.assertEqual(
                self.app_module._resolve_active_creator_id(),
                'c1',
            )

    def test_host_match_normalizes_case_and_port(self):
        self._insert_creator(
            id='c1', shop_domain='Shop.Test-Creator.Example.com',
        )
        with self.app_module.app.test_request_context(
            '/', base_url='http://shop.test-creator.example.com:8443',
        ):
            self.assertEqual(
                self.app_module._resolve_active_creator_id(),
                'c1',
            )

    def test_default_when_no_env_and_no_host_match(self):
        with self.app_module.app.test_request_context(
            '/', base_url='http://something-unmapped.example.com',
        ):
            self.assertEqual(
                self.app_module._resolve_active_creator_id(),
                'everydaywithsteph',
            )

    def test_env_beats_host_match(self):
        self._insert_creator(id='c1', shop_domain='shop.test-creator.example.com')
        os.environ['ACTIVE_CREATOR_ID'] = 'env-creator'
        try:
            with self.app_module.app.test_request_context(
                '/', base_url='http://shop.test-creator.example.com',
            ):
                self.assertEqual(
                    self.app_module._resolve_active_creator_id(),
                    'env-creator',
                )
        finally:
            os.environ.pop('ACTIVE_CREATOR_ID', None)


# ── 2. branding loader ────────────────────────────────────────────────────────

class BrandingLoader(_BoundaryTestBase):

    def test_missing_directory_returns_empty(self):
        self._set_branding_dir_missing()
        self.assertEqual(self.app_module._load_branding_overrides(), {})

    def test_partial_directory_logo_only(self):
        logo_path = os.path.join(self.branding_dir, 'logo.png')
        with open(logo_path, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n')  # 8-byte PNG header is enough
        self.app_module._branding_cache_reset()
        overrides = self.app_module._load_branding_overrides()
        self.assertEqual(overrides.get('logo_url'), '/branding/logo.png')
        self.assertNotIn('favicon_url', overrides)
        # No overrides.json → no color/shop_name keys
        self.assertNotIn('brand_primary', overrides)

    def test_logo_first_match_wins(self):
        # Both .png and .svg present — .png is first in candidate tuple.
        for name in ('logo.png', 'logo.svg'):
            with open(os.path.join(self.branding_dir, name), 'wb') as f:
                f.write(b'x')
        self.app_module._branding_cache_reset()
        overrides = self.app_module._load_branding_overrides()
        self.assertEqual(overrides.get('logo_url'), '/branding/logo.png')

    def test_favicon_url_is_root_relative(self):
        with open(os.path.join(self.branding_dir, 'favicon.ico'), 'wb') as f:
            f.write(b'\x00\x00')
        self.app_module._branding_cache_reset()
        overrides = self.app_module._load_branding_overrides()
        self.assertEqual(overrides.get('favicon_url'), '/branding/favicon.ico')

    def test_valid_overrides_json_merges(self):
        payload = {'brand_primary': '#abc123', 'shop_name': 'Custom Shop'}
        with open(os.path.join(self.branding_dir, 'overrides.json'), 'w') as f:
            json.dump(payload, f)
        self.app_module._branding_cache_reset()
        overrides = self.app_module._load_branding_overrides()
        self.assertEqual(overrides.get('brand_primary'), '#abc123')
        self.assertEqual(overrides.get('shop_name'), 'Custom Shop')

    def test_malformed_overrides_json_does_not_raise(self):
        with open(os.path.join(self.branding_dir, 'overrides.json'), 'w') as f:
            f.write('{not valid json')
        # File-asset side still picks up logo.
        with open(os.path.join(self.branding_dir, 'logo.png'), 'wb') as f:
            f.write(b'\x89PNG')
        self.app_module._branding_cache_reset()
        overrides = self.app_module._load_branding_overrides()
        self.assertEqual(overrides.get('logo_url'), '/branding/logo.png')
        # Malformed JSON → its keys are absent, but loader returned cleanly.
        self.assertNotIn('brand_primary', overrides)

    def test_overrides_json_non_object_root_is_tolerated(self):
        with open(os.path.join(self.branding_dir, 'overrides.json'), 'w') as f:
            json.dump(['not', 'an', 'object'], f)
        self.app_module._branding_cache_reset()
        # Should not raise.
        overrides = self.app_module._load_branding_overrides()
        self.assertIsInstance(overrides, dict)


# ── 3. brand context precedence ───────────────────────────────────────────────

class BrandContextPrecedence(_BoundaryTestBase):

    def test_active_row_overrides_demo_defaults(self):
        self._insert_creator(
            id='c1',
            display_name='Other',
            brand_label='Other Brand',
            brand_primary='#111111',
        )
        ctx = self.app_module.build_brand_context('c1')
        self.assertEqual(ctx['brand_label'], 'Other Brand')
        self.assertEqual(ctx['brand_primary'], '#111111')

    def test_overrides_json_beats_active_row(self):
        self._insert_creator(
            id='c1', brand_label='Row Brand', brand_primary='#111111',
        )
        with open(os.path.join(self.branding_dir, 'overrides.json'), 'w') as f:
            json.dump({'brand_primary': '#ff0000', 'shop_name': 'JSON Shop'}, f)
        self.app_module._branding_cache_reset()
        ctx = self.app_module.build_brand_context('c1')
        self.assertEqual(ctx['brand_primary'], '#ff0000')
        self.assertEqual(ctx['shop_name'], 'JSON Shop')

    def test_demo_row_used_when_active_row_missing_field(self):
        # Active row exists but with NO brand_label set; falls through.
        # Note: db_schema.get_creator() returns DEFAULT_CREATOR dict for
        # an unknown id, so the demo row is the floor regardless.
        ctx = self.app_module.build_brand_context('does-not-exist')
        # DEFAULT_CREATOR has brand_label = 'Mommy & Me Collective'.
        self.assertEqual(ctx['brand_label'], 'Mommy & Me Collective')

    def test_framework_defaults_when_demo_field_is_null(self):
        # Demo row has brand_primary = None (intentional per seed).
        ctx = self.app_module.build_brand_context('everydaywithsteph')
        self.assertIsNone(ctx['brand_primary'])
        # And the resolver-supplied creator_id is preserved.
        self.assertEqual(ctx['creator_id'], 'everydaywithsteph')

    def test_shop_name_falls_back_to_brand_label(self):
        # No override, no shop_name column — derives from brand_label.
        ctx = self.app_module.build_brand_context('everydaywithsteph')
        self.assertEqual(ctx['shop_name'], 'Mommy & Me Collective')

    def test_brand_surface_pair_present_in_context(self):
        # K1 — context must expose both canvas/surface keys even when null.
        ctx = self.app_module.build_brand_context('everydaywithsteph')
        self.assertIn('brand_surface', ctx)
        self.assertIn('brand_on_surface', ctx)
        # Demo creator does not seed colors; both keys stay None.
        self.assertIsNone(ctx['brand_surface'])
        self.assertIsNone(ctx['brand_on_surface'])

    def test_brand_surface_active_row_overrides_demo(self):
        # K1 — when the active creator row sets the surface pair, the
        # context picks them up over the demo row's NULLs.
        self._insert_creator(
            id='c-canvas',
            display_name='Canvas Creator',
            brand_surface='#e5dbc8',
            brand_on_surface='#1a1a17',
        )
        ctx = self.app_module.build_brand_context('c-canvas')
        self.assertEqual(ctx['brand_surface'], '#e5dbc8')
        self.assertEqual(ctx['brand_on_surface'], '#1a1a17')

    def test_brand_surface_overrides_json_beats_active_row(self):
        # K1 — overrides.json layer still wins over creator row for the
        # surface pair (same precedence as brand_primary).
        self._insert_creator(
            id='c-canvas',
            display_name='Canvas Creator',
            brand_surface='#000001',
            brand_on_surface='#fffffe',
        )
        with open(os.path.join(self.branding_dir, 'overrides.json'), 'w') as f:
            json.dump({
                'brand_surface': '#e5dbc8',
                'brand_on_surface': '#1a1a17',
            }, f)
        self.app_module._branding_cache_reset()
        ctx = self.app_module.build_brand_context('c-canvas')
        self.assertEqual(ctx['brand_surface'], '#e5dbc8')
        self.assertEqual(ctx['brand_on_surface'], '#1a1a17')


# ── 4. context_processor wiring ───────────────────────────────────────────────

class ContextProcessor(_BoundaryTestBase):

    def test_brand_reaches_rendered_template(self):
        from flask import render_template_string
        with self.app_module.app.test_request_context('/'):
            out = render_template_string('{{ brand.handle }}')
            # DEFAULT_CREATOR.handle = '@EverydaywithSteph'
            self.assertIn('@EverydaywithSteph', out)

    def test_g_active_creator_id_is_stamped(self):
        client = self.app_module.app.test_client()
        # /healthz takes the cheap path — but before_request still runs.
        # We'll exercise via test_request_context to inspect g directly.
        with self.app_module.app.test_request_context('/'):
            # Fire before_request hooks manually:
            self.app_module.app.preprocess_request()
            from flask import g
            self.assertEqual(g.active_creator_id, 'everydaywithsteph')


# ── 5. CSS variable rendering ─────────────────────────────────────────────────

class BrandVarsTemplate(_BoundaryTestBase):

    def test_null_creator_color_renders_fallback(self):
        from flask import render_template
        with self.app_module.app.test_request_context('/'):
            css = render_template('partials/_brand_vars.html')
        # Demo creator brand_primary is None → fallback chain visible.
        self.assertIn('--brand-primary:', css)
        self.assertIn('var(--accent, #e85d26)', css)
        self.assertIn('var(--card,   #ffffff)', css)

    def test_non_null_creator_color_appears_literally(self):
        from flask import render_template, g
        with self.app_module.app.test_request_context('/'):
            ctx = self.app_module.build_brand_context('everydaywithsteph')
            ctx['brand_primary'] = '#abc123'
            ctx['brand_on_primary'] = '#fefefe'
            g._brand_ctx = ctx
            css = render_template('partials/_brand_vars.html')
        # Literal hex appears on the property line.
        primary_line = [
            line for line in css.splitlines()
            if '--brand-primary:' in line and '--brand-primary-' not in line
        ][0]
        self.assertIn('#abc123', primary_line)
        self.assertNotIn('var(--accent', primary_line)


# ── 6. /branding/<path:filename> route ────────────────────────────────────────

class BrandingAssetRoute(_BoundaryTestBase):

    def test_serves_existing_asset(self):
        logo_path = os.path.join(self.branding_dir, 'logo.png')
        body = b'\x89PNG\r\n\x1a\n-test-bytes'
        with open(logo_path, 'wb') as f:
            f.write(body)
        client = self.app_module.app.test_client()
        resp = client.get('/branding/logo.png')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, body)

    def test_returns_404_when_file_missing(self):
        client = self.app_module.app.test_client()
        resp = client.get('/branding/no-such-file.png')
        self.assertEqual(resp.status_code, 404)

    def test_returns_404_when_directory_missing(self):
        self._set_branding_dir_missing()
        client = self.app_module.app.test_client()
        resp = client.get('/branding/logo.png')
        self.assertEqual(resp.status_code, 404)


# ── 7. missing branding/ keeps storefront alive ───────────────────────────────

class MissingBrandingDoesNotBreakRender(_BoundaryTestBase):

    def test_storefront_render_with_no_branding_dir(self):
        self._set_branding_dir_missing()
        from flask import render_template_string
        with self.app_module.app.test_request_context('/'):
            # context_processor should still produce a brand dict; missing
            # branding/ should not trigger a 500 in template rendering.
            out = render_template_string(
                '{{ brand.handle }}|{{ brand.shop_name }}|'
                '{{ brand.logo_url or "no-logo" }}'
            )
        self.assertIn('@EverydaywithSteph', out)
        # Jinja HTML-escapes '&' → 'Mommy &amp; Me Collective' in default
        # autoescape mode. Match the rendered form, not the raw value.
        self.assertIn('Mommy &amp; Me Collective', out)
        # logo_url falls through to creator-row value (the demo asset path).
        # If overrides.json is missing too, we just take whatever the row has;
        # demo row has logo_url = 'static/images/mmc-og-preview.png'.
        self.assertIn('mmc-og-preview.png', out)

    def test_healthz_still_responds(self):
        self._set_branding_dir_missing()
        client = self.app_module.app.test_client()
        resp = client.get('/healthz')
        self.assertEqual(resp.status_code, 200)


# ── 8. /branding/ extension whitelist ─────────────────────────────────────────
# Codex audit follow-up: the route originally served any file in branding/,
# including overrides.json. Lock to known asset extensions so per-deploy
# config can never leak publicly even when an operator drops a sensitive
# file in branding/ by accident.

class BrandingAssetExtensionWhitelist(_BoundaryTestBase):

    def test_overrides_json_is_not_publicly_servable(self):
        # overrides.json exists on disk and is loaded by the override loader,
        # but the public asset route must refuse to serve it.
        payload = {'brand_primary': '#abc123'}
        with open(os.path.join(self.branding_dir, 'overrides.json'), 'w') as f:
            import json as _json
            _json.dump(payload, f)
        client = self.app_module.app.test_client()
        resp = client.get('/branding/overrides.json')
        self.assertEqual(resp.status_code, 404)
        # And the body shouldn't contain the secret hex either.
        self.assertNotIn(b'#abc123', resp.data)

    def test_other_non_asset_extensions_404(self):
        # Drop a .txt and a .yaml — both should be blocked.
        for name, body in (('config.txt', b'secret'), ('settings.yaml', b'k: v')):
            with open(os.path.join(self.branding_dir, name), 'wb') as f:
                f.write(body)
        client = self.app_module.app.test_client()
        for name in ('config.txt', 'settings.yaml'):
            resp = client.get(f'/branding/{name}')
            self.assertEqual(
                resp.status_code, 404,
                f'{name} should be blocked by the extension whitelist',
            )

    def test_allowed_image_extensions_still_serve(self):
        # Sanity: the whitelist allows the asset types we actually need.
        for name in ('logo.png', 'logo.svg', 'logo.webp', 'favicon.ico'):
            with open(os.path.join(self.branding_dir, name), 'wb') as f:
                f.write(b'\x00')
        client = self.app_module.app.test_client()
        for name in ('logo.png', 'logo.svg', 'logo.webp', 'favicon.ico'):
            resp = client.get(f'/branding/{name}')
            self.assertEqual(
                resp.status_code, 200,
                f'{name} should be servable under the whitelist',
            )

    def test_extension_check_is_case_insensitive(self):
        # Operators sometimes drop LOGO.PNG; the whitelist must accept it.
        with open(os.path.join(self.branding_dir, 'LOGO.PNG'), 'wb') as f:
            f.write(b'\x89PNG')
        client = self.app_module.app.test_client()
        resp = client.get('/branding/LOGO.PNG')
        self.assertEqual(resp.status_code, 200)


# ── 9. end-to-end render — brand color reaches the response body ─────────────
# Codex audit follow-up: this is the test that would have caught the
# wiring gap on its own. We exercise a real storefront route (/shop/<slug>),
# host-resolved to a creator with non-NULL brand_primary, and assert the
# literal hex appears in the rendered HTML.

class EndToEndBrandRender(_BoundaryTestBase):

    def _seed_collage(self, slug='sage-test', creator_id='c-sage'):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO collages
                (slug, products_json, caption, creator_id, status, hero_title, click_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (slug, json.dumps([]), 'caption', creator_id, 'published',
                 'Sage Test', 0),
            )
            conn.commit()
        finally:
            conn.close()

    def test_brand_primary_hex_appears_in_storefront_response(self):
        # Creator row carries brand_primary=#7c7d6a and a unique shop_domain
        # the resolver will match.
        self._insert_creator(
            id='c-sage',
            display_name='Sage Creator',
            shop_domain='shop.sage-test.example.com',
            brand_primary='#7c7d6a',
            brand_primary_container='#ddbba4',
        )
        self._seed_collage(slug='sage-collection', creator_id='c-sage')

        client = self.app_module.app.test_client()
        resp = client.get(
            '/shop/sage-collection',
            base_url='http://shop.sage-test.example.com',
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        # The literal brand hex must appear (it's in the _brand_vars.html
        # bridge that the template now includes).
        self.assertIn(
            '#7c7d6a', body,
            "brand_primary hex should appear in rendered storefront response",
        )
        self.assertIn(
            '#ddbba4', body,
            "brand_primary_container hex should appear in rendered response",
        )
        # And the legacy --accent override line should be present.
        self.assertIn('--accent: #7c7d6a', body)

    def test_null_brand_primary_falls_back_to_creator_core(self):
        # Same setup but no brand_primary set — must NOT have the bridge
        # active, and the rendered page should still paint with theme.accent
        # (Creator Core peach #e85d26 is the default theme; if collage.theme
        # is unset, the route falls back to 'peach').
        self._insert_creator(
            id='c-null',
            display_name='Null Creator',
            shop_domain='shop.null-test.example.com',
            brand_primary=None,
        )
        self._seed_collage(slug='null-collection', creator_id='c-null')

        client = self.app_module.app.test_client()
        resp = client.get(
            '/shop/null-collection',
            base_url='http://shop.null-test.example.com',
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        # Bridge must be inert when brand_primary is NULL — the
        # legacy --accent override line must NOT appear.
        self.assertNotIn(
            '--accent: #', body.split('/* P0.7 legacy', 1)[-1].split('}', 1)[0]
            if '/* P0.7 legacy' in body else '',
            'Bridge must not emit --accent override when brand_primary is NULL',
        )
        # And the brand-vars partial's static fallback chain for
        # --brand-primary must be present.
        self.assertIn('var(--accent, #e85d26)', body)


# ── 10. _brand_vars.html is actually included by live storefront templates ───
# Static guard against regression: if someone removes the include from a
# storefront template, this test fails before runtime.

class StorefrontIncludesBrandVars(_BoundaryTestBase):

    LIVE_TEMPLATES = (
        'shop_landing.html',
        'shop_directory.html',
        'shop_posts.html',
        'walmart_trending_now.html',
    )

    def test_every_live_storefront_template_includes_brand_vars(self):
        template_dir = os.path.join(
            os.path.dirname(os.path.abspath(self.app_module.__file__)),
            'templates',
        )
        for name in self.LIVE_TEMPLATES:
            path = os.path.join(template_dir, name)
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn(
                "include 'partials/_brand_vars.html'", content,
                f'{name} must {{% include %}} partials/_brand_vars.html — '
                f'otherwise brand columns never reach the rendered page.',
            )


if __name__ == '__main__':
    unittest.main()
