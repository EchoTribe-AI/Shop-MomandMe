"""Tests for production fail-closed behavior when admin config is missing.

When the app runs against a production-style backend (DATABASE_URL set,
FLASK_ENV != 'development') but SECRET_KEY or ADMIN_PASSWORD is missing:

  - /healthz still returns 200 (Cloud Run probe stays green).
  - Public storefront routes still work (visitors aren't blocked).
  - Every admin path (page or API) returns 503 with a clear message.

The fail-closed check runs at request time, so each test patches the
environment and reloads app.py inside the patched context.
"""

import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from unittest import mock


@contextmanager
def _app_with_env(env: dict):
    """Reload app.py under a clean env and keep the env patched for the
    duration of the with-block (so request-time env reads see the same
    values as module-import-time reads)."""
    # Drop any pre-existing import so module-level config recomputes.
    if 'app' in sys.modules:
        del sys.modules['app']
    with mock.patch.dict(os.environ, env, clear=True):
        import app as app_mod  # noqa: WPS433 — intentional dynamic reload
        importlib.reload(app_mod)
        yield app_mod


# Env presets ──────────────────────────────────────────────────────────────────

PROD_MISSING_BOTH = {
    'DATABASE_URL': 'postgresql://fake/local',
    # FLASK_ENV intentionally unset → treated as production.
    # SECRET_KEY and ADMIN_PASSWORD intentionally unset.
}

PROD_MISSING_SECRET_KEY = {
    'DATABASE_URL': 'postgresql://fake/local',
    'ADMIN_PASSWORD': 'test-password',
}

PROD_MISSING_ADMIN_PASSWORD = {
    'DATABASE_URL': 'postgresql://fake/local',
    'SECRET_KEY': 'test-secret',
}

PROD_OK = {
    'DATABASE_URL': 'postgresql://fake/local',
    'SECRET_KEY': 'test-secret-not-the-real-one',
    'ADMIN_PASSWORD': 'test-password',
}

DEV_ENV = {
    'FLASK_ENV': 'development',
    # No DATABASE_URL, no SECRET_KEY, no ADMIN_PASSWORD.
}


class FailClosedWhenProdMissingSecrets(unittest.TestCase):
    """In prod (DATABASE_URL set, FLASK_ENV != development), missing secrets
    must hard-stop the admin surface while leaving the rest of the app up."""

    def test_admin_config_missing_reports_both(self):
        with _app_with_env(PROD_MISSING_BOTH) as app_mod:
            missing = app_mod._admin_config_missing()
        self.assertIn('SECRET_KEY', missing)
        self.assertIn('ADMIN_PASSWORD', missing)

    def test_admin_config_missing_reports_only_secret_key(self):
        with _app_with_env(PROD_MISSING_SECRET_KEY) as app_mod:
            missing = app_mod._admin_config_missing()
        self.assertEqual(missing, ['SECRET_KEY'])

    def test_admin_config_missing_reports_only_admin_password(self):
        with _app_with_env(PROD_MISSING_ADMIN_PASSWORD) as app_mod:
            missing = app_mod._admin_config_missing()
        self.assertEqual(missing, ['ADMIN_PASSWORD'])

    def test_healthz_still_returns_200(self):
        with _app_with_env(PROD_MISSING_BOTH) as app_mod:
            client = app_mod.app.test_client()
            resp = client.get('/healthz')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'ok', resp.data.lower())

    def test_admin_page_guard_returns_503(self):
        with _app_with_env(PROD_MISSING_BOTH) as app_mod:
            client = app_mod.app.test_client()
            resp = client.get('/hub', follow_redirects=False)
        self.assertEqual(resp.status_code, 503)
        body = resp.data.decode('utf-8', errors='ignore')
        self.assertIn('missing production config', body)
        self.assertIn('SECRET_KEY', body)
        self.assertIn('ADMIN_PASSWORD', body)

    def test_admin_login_route_returns_503(self):
        """Even the login form refuses to render so admins don't submit a
        password that could never authenticate."""
        with _app_with_env(PROD_MISSING_BOTH) as app_mod:
            client = app_mod.app.test_client()
            resp = client.get('/admin/login', follow_redirects=False)
        self.assertEqual(resp.status_code, 503)
        self.assertIn('missing production config', resp.data.decode('utf-8'))

    def test_admin_api_guard_returns_503_json(self):
        """JSON admin APIs must 503 with a parseable error payload."""
        with _app_with_env(PROD_MISSING_BOTH) as app_mod:
            client = app_mod.app.test_client()
            resp = client.post('/admin/walmart-trends/refresh')
        self.assertEqual(resp.status_code, 503)
        payload = resp.get_json(silent=True) or {}
        self.assertEqual(payload.get('error'), 'missing production admin config')
        self.assertIn('SECRET_KEY', payload.get('missing', []))


class FullyConfiguredProdAllowsAdmin(unittest.TestCase):
    """When both required env vars are set, the fail-closed branch is
    dormant and the previous auth behavior applies."""

    def test_admin_config_missing_is_empty(self):
        with _app_with_env(PROD_OK) as app_mod:
            self.assertEqual(app_mod._admin_config_missing(), [])

    def test_admin_login_renders(self):
        with _app_with_env(PROD_OK) as app_mod:
            client = app_mod.app.test_client()
            resp = client.get('/admin/login')
        # 200 (form) or 302 (already-authed redirect) — anything but 503.
        self.assertNotEqual(resp.status_code, 503)


class UrlTokenAuthRejected(unittest.TestCase):
    """Audit follow-up 0.3: ?admin_token= URL query-string auth has been
    removed from both _require_walmart_trends_admin (JSON API guard) and
    _require_admin_page (HTML page guard). Only session OR header auth
    works for the API guard; only session works for the page guard."""

    URL_TOKEN_PROD_ENV = {
        'DATABASE_URL': 'postgresql://fake/local',
        'SECRET_KEY': 'test-secret',
        'ADMIN_PASSWORD': 'test-password',
        'WALMART_TRENDS_ADMIN_TOKEN': 'super-secret-trends-token',
    }

    def test_page_url_token_does_not_create_session(self):
        """Previously /hub?admin_token=<correct-token> would set a 30-day
        admin session. That conversion path is now removed: the request
        must redirect to /admin/login."""
        with _app_with_env(self.URL_TOKEN_PROD_ENV) as app_mod:
            client = app_mod.app.test_client()
            resp = client.get(
                '/hub?admin_token=super-secret-trends-token',
                follow_redirects=False,
            )
        # Either redirect to /admin/login (302) or some other non-200.
        # Critically NOT 200 (which would mean URL-token auth still works).
        self.assertNotEqual(resp.status_code, 200)
        if resp.status_code == 302:
            self.assertIn('/admin/login', resp.headers.get('Location', ''))

    # Use a production-style Host header so _walmart_content_demo_allowed()
    # doesn't short-circuit auth via the localhost dev branch. Avoid the
    # SHOP_SUBDOMAIN ("shop.echotribe.ai") because that triggers the
    # public-subdomain rewrite middleware which 404s admin paths.
    PROD_HOST = 'admin.echotribe.ai'

    def test_api_url_token_is_rejected(self):
        """Previously POST /admin/walmart-trends/refresh?admin_token=...
        would authenticate via query string. Now the only accepted ways
        are session cookie or X-Walmart-Trends-Admin-Token header."""
        with _app_with_env(self.URL_TOKEN_PROD_ENV) as app_mod:
            client = app_mod.app.test_client()
            resp = client.post(
                '/admin/walmart-trends/refresh?admin_token=super-secret-trends-token',
                headers={'Host': self.PROD_HOST},
                base_url=f'http://{self.PROD_HOST}',
            )
        self.assertEqual(resp.status_code, 401)

    def test_api_header_token_still_works(self):
        """Header-based auth must continue working for cron job automation."""
        with _app_with_env(self.URL_TOKEN_PROD_ENV) as app_mod:
            client = app_mod.app.test_client()
            resp = client.post(
                '/admin/walmart-trends/refresh',
                headers={
                    'X-Walmart-Trends-Admin-Token': 'super-secret-trends-token',
                    'Host': self.PROD_HOST,
                },
                base_url=f'http://{self.PROD_HOST}',
            )
        # Auth check passes; downstream may fail because no real Impact
        # API is wired up in test, but the response code should not be
        # 401 (unauthorized) or 503 (missing config).
        self.assertNotIn(resp.status_code, (401, 503))


class ArcherRoutesGuarded(unittest.TestCase):
    """Audit follow-up 0.5: every /archer/* route except /archer/track_click
    must require admin auth. Previously /archer/products, /archer/search,
    /archer/ads (and ~30 others) returned 200 unauthenticated."""

    GUARDED_ENV = {
        'DATABASE_URL': 'postgresql://fake/local',
        'SECRET_KEY': 'test-secret',
        'ADMIN_PASSWORD': 'test-password',
        'WALMART_TRENDS_ADMIN_TOKEN': 'test-token',
    }
    PROD_HOST = 'admin.echotribe.ai'  # avoid SHOP_SUBDOMAIN rewrite

    def _client_and_prod(self, app_mod):
        return app_mod.app.test_client()

    def _prod_headers(self):
        return {'Host': self.PROD_HOST}

    def _base_url(self):
        return f'http://{self.PROD_HOST}'

    # test_archer_products_page_redirects_unauth removed in the
    # Shop-MomandMe strip-down — /archer/products HTML page deleted.
    # The /archer/product/<asin> JSON variant (KEEP) is covered by
    # test_archer_product_json_returns_401_unauth below.

    def test_archer_search_returns_401_unauth(self):
        """JSON route — guard returns 401."""
        with _app_with_env(self.GUARDED_ENV) as app_mod:
            client = self._client_and_prod(app_mod)
            resp = client.get(
                '/archer/search?q=test',
                headers=self._prod_headers(),
                base_url=self._base_url(),
            )
        self.assertEqual(resp.status_code, 401)

    def test_archer_product_json_returns_401_unauth(self):
        """The /archer/product/<asin> JSON variant is used by archer_collage's
        JS — but archer_collage is itself session-gated, so the JS call
        rides on the user's session. Unauthed callers must be denied."""
        with _app_with_env(self.GUARDED_ENV) as app_mod:
            client = self._client_and_prod(app_mod)
            resp = client.get(
                '/archer/product/B0TEST',
                headers=self._prod_headers(),
                base_url=self._base_url(),
            )
        self.assertEqual(resp.status_code, 401)

    # test_archer_ads_page_redirects_unauth removed in the
    # Shop-MomandMe strip-down — /archer/ads page deleted.

    def test_archer_track_click_remains_public(self):
        """/archer/track_click is intentionally public — used by storefront
        JS to log click-through events. Must NOT be guarded."""
        with _app_with_env(self.GUARDED_ENV) as app_mod:
            client = self._client_and_prod(app_mod)
            resp = client.post(
                '/archer/track_click',
                json={'asin': 'B0TEST', 'slug': 'test-slug'},
                headers=self._prod_headers(),
                base_url=self._base_url(),
            )
        # Auth-related codes must NOT be returned. Downstream insert may
        # fail in test env (no schema initialized) but the route should
        # not return 401/403/503/302-to-login.
        self.assertNotIn(resp.status_code, (302, 401, 403))

    def test_authed_session_unblocks_archer(self):
        """Sanity: with a real admin session, /archer/collage renders.

        Uses the default localhost host (no PROD_HOST override) so the
        Flask test client's cookie jar uses the same domain for setting
        and reading the session cookie. The session check fires before
        the localhost dev-allowance branch in any case.

        Was /archer/products pre-strip-down; switched to /archer/collage
        which is the canonical KEEP page on Shop-MomandMe."""
        with _app_with_env(self.GUARDED_ENV) as app_mod:
            client = self._client_and_prod(app_mod)
            with client.session_transaction() as sess:
                sess['admin_authed'] = True
            resp = client.get('/archer/collage')
        self.assertEqual(resp.status_code, 200)

    def test_header_token_unblocks_archer_api(self):
        """Cron/automation token (header) must continue to authenticate
        /archer/* JSON routes."""
        with _app_with_env(self.GUARDED_ENV) as app_mod:
            client = self._client_and_prod(app_mod)
            resp = client.get(
                '/archer/search?q=test',
                headers={
                    'Host': self.PROD_HOST,
                    'X-Walmart-Trends-Admin-Token': 'test-token',
                },
                base_url=self._base_url(),
            )
        self.assertNotEqual(resp.status_code, 401)


class DevModeAllowsDefaults(unittest.TestCase):
    """In dev, the fail-closed check is a no-op even with missing config."""

    def test_admin_config_missing_is_empty_in_dev(self):
        with _app_with_env(DEV_ENV) as app_mod:
            self.assertEqual(app_mod._admin_config_missing(), [])

    def test_admin_login_works_with_defaults_in_dev(self):
        with _app_with_env(DEV_ENV) as app_mod:
            client = app_mod.app.test_client()
            resp = client.get('/admin/login')
        self.assertNotEqual(resp.status_code, 503)


if __name__ == '__main__':
    unittest.main()
