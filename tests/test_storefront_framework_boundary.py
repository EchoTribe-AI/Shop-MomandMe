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


if __name__ == '__main__':
    unittest.main()
