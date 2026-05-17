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
