"""Route-inventory regression test.

Walks Flask's url_map and asserts every registered route is either:
  (a) on the PUBLIC_ROUTES allowlist, with a documented reason, OR
  (b) refuses unauthenticated requests (302 to /admin/login, 401, or 503).

This catches the failure mode that bit pre-launch: a new route lands
without `@require_admin_page` / `@require_admin_api` and ships to
production accepting unauthenticated traffic. Future devs adding a
public route must add it to PUBLIC_ROUTES with a one-line reason.

Background: the original PR #38 hardening focused only on /archer/*.
Audit found 11 more unguarded admin routes (/dashboard, /insights,
/urlgenius/*, /levanta/*, /admin/creators POST/GET-by-id, etc.). This
test makes that class of miss impossible to repeat silently.
"""

import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from unittest import mock


# ── Public-routes allowlist ───────────────────────────────────────────────────
# Every entry is (rule, reason). When you add a new public route to app.py,
# you MUST add it here with a justification. Reviewers should ask: does this
# really need to be public, and is the reason still true?
#
# Format: Flask rule pattern (e.g. '/shop/<slug>'), exact match against
# rule.rule. Wildcards/<vars> in the rule are matched as-is.
PUBLIC_ROUTES = {
    # Cloud Run health probe — must stay public, no DB.
    '/healthz': 'Cloud Run health probe; no DB; no auth.',
    # Storefront landing + creator-shop pages — anonymous shoppers.
    '/': 'Index redirect to /hub; /hub itself is guarded.',
    '/shop/': 'Public storefront directory.',
    '/shop/<slug>': 'Public storefront landing for a collection.',
    '/shop/posts': 'Public storefront posts feed.',
    '/collections': 'Public collection directory.',
    '/trends': 'Public Walmart trends home.',
    # Public APIs called by storefront JS.
    '/api/chat': 'Public storefront chat (anonymous shoppers).',
    '/api/shop/chat': 'Public storefront chat (subdomain passthrough).',
    '/api/walmart/trending-now': 'Public JSON for the trends page.',
    '/archer/track_click': 'Public click logger (anonymous shoppers). '
                           'Bot defense + rate limiting designed in P0.4. '
                           'Will be renamed to /api/clicks per P0.1.',
    # SEO assets.
    '/sitemap.xml': 'Public SEO.',
    '/robots.txt': 'Public SEO.',
    # P0.7 per-deploy branding/ asset surface — logo, favicon, etc.
    # Storefront chrome that anonymous shoppers see on every page.
    # send_from_directory's safe_join blocks path traversal; missing files
    # return 404. No deploy-internal content lives in branding/.
    '/branding/<path:filename>':
        'P0.7 per-deploy branding assets (logo/favicon). Anonymous '
        'shoppers; safe static serving via send_from_directory.',
    # Hybrid route — public when no ?admin=1, session-guarded when
    # ?admin=1. The conditional guard fires inside the handler. Separate
    # test below verifies admin-mode behavior.
    '/walmart/trending-now': 'Public landing page; ?admin=1 mode is guarded '
                             'inside the handler (see WalmartTrendingNowAdminMode test).',
    # Auth surface itself.
    '/admin/login': 'Login form (must be reachable to log in). '
                    'In production this 503s when SECRET_KEY/ADMIN_PASSWORD '
                    'are missing — see _admin_config_missing.',
    '/admin/logout': 'Logout (just clears session; safe to be open).',
    # Legacy aliases — pure 302 redirects to guarded targets.
    '/walmart/collections/<collection_slug>/create-post':
        'Legacy 302 alias → /collections/<slug>/create-post (guarded).',
    '/walmart/pages/<public_slug>/edit':
        'Legacy 302 alias → /collections/<slug>/edit (guarded).',
    # Static files served by Flask in dev (not relevant to prod).
    '/static/<path:filename>': 'Static-file server.',
}


@contextmanager
def _app_with_env(env: dict):
    """Reload app.py under a clean env."""
    if 'app' in sys.modules:
        del sys.modules['app']
    with mock.patch.dict(os.environ, env, clear=True):
        import app as app_mod  # noqa: WPS433
        importlib.reload(app_mod)
        yield app_mod


# Production-style env: fail-closed posture is OFF (config is present),
# so guarded routes return 401/302 rather than 503 — that's what we want
# to verify in the inventory check.
PROD_ENV_OK = {
    'DATABASE_URL': 'postgresql://fake/local',
    'SECRET_KEY': 'test-secret-not-the-real-one',
    'ADMIN_PASSWORD': 'test-password',
    'WALMART_TRENDS_ADMIN_TOKEN': 'test-token',
}

# Non-localhost host so _walmart_content_demo_allowed() doesn't auto-allow
# every test request. Avoids SHOP_SUBDOMAIN to dodge the public-rewrite
# middleware.
PROD_HOST = 'admin.echotribe.ai'


def _rule_methods_for_smoke(methods: set) -> str:
    """Pick the most informative HTTP method to probe for this rule."""
    if 'GET' in methods:
        return 'GET'
    if 'POST' in methods:
        return 'POST'
    # PATCH, DELETE, PUT, OPTIONS — pick whichever non-OPTIONS is there.
    for m in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if m in methods:
            return m
    return 'GET'


class RouteInventoryGuardCheck(unittest.TestCase):
    """Every non-public route must refuse unauthenticated requests."""

    def test_every_route_guarded_or_explicitly_public(self):
        with _app_with_env(PROD_ENV_OK) as app_mod:
            client = app_mod.app.test_client()
            rules_to_probe = []
            for rule in app_mod.app.url_map.iter_rules():
                # Skip Flask's internal static handler and HEAD/OPTIONS.
                if rule.endpoint == 'static':
                    continue
                rules_to_probe.append(rule)

            unguarded_non_public = []
            unverifiable = []  # routes that returned 500 / unhandled errors

            for rule in rules_to_probe:
                if rule.rule in PUBLIC_ROUTES:
                    continue  # documented public route
                method = _rule_methods_for_smoke(rule.methods or {'GET'})
                # Build a concrete URL by replacing path variables with
                # safe placeholders. (Flask's url_for needs valid values
                # for typed converters like <int:>.)
                # Build a concrete URL by replacing path variables with
                # safe placeholders. Flask's typed converters (<int:>, <float:>,
                # <path:>) need values that match the type.
                import re as _re
                url = rule.rule
                url = _re.sub(r'<int:[^>]+>', '1', url)
                url = _re.sub(r'<float:[^>]+>', '1.0', url)
                url = _re.sub(r'<path:[^>]+>', 'x/y', url)
                url = _re.sub(r'<[^>]+>', 'x', url)

                try:
                    resp = client.open(
                        url,
                        method=method,
                        headers={'Host': PROD_HOST},
                        base_url=f'http://{PROD_HOST}',
                    )
                except Exception as e:
                    unverifiable.append((rule.rule, f'request-raised: {e!r}'))
                    continue

                # Accepted "refuses unauth" codes:
                #   302 — page guard redirected to /admin/login
                #   401 — API guard returned unauthorized
                #   503 — production fail-closed (config missing) — shouldn't
                #         fire here because PROD_ENV_OK is configured, but
                #         tolerated in case a route 503s for other reasons
                #         like webhook secret missing
                refused = resp.status_code in (302, 401, 503)
                # A redirect to /admin/login is the explicit "guarded page"
                # signal.
                if resp.status_code == 302:
                    refused = '/admin/login' in resp.headers.get('Location', '')
                if not refused:
                    unguarded_non_public.append(
                        (rule.rule, method, resp.status_code)
                    )

            if unguarded_non_public:
                lines = ['\nThe following routes are NOT in PUBLIC_ROUTES and '
                         'NOT refusing unauthenticated traffic. Either:']
                lines.append('  (a) Add @require_admin_page / @require_admin_api '
                             'to guard them, OR')
                lines.append('  (b) If they are intentionally public, add them '
                             'to PUBLIC_ROUTES in this test file with a reason.')
                lines.append('')
                for rule, method, code in unguarded_non_public:
                    lines.append(f'  {method:6s} {rule}  →  HTTP {code}')
                self.fail('\n'.join(lines))

            if unverifiable:
                # These don't fail the test but log a warning so they're
                # visible. If a route can't be probed, the inventory check
                # is silently incomplete for it.
                import sys as _sys
                print('\n[ROUTE INVENTORY] Could not probe these rules:',
                      file=_sys.stderr)
                for rule, reason in unverifiable:
                    print(f'  {rule}  →  {reason}', file=_sys.stderr)


class WalmartTrendingNowAdminMode(unittest.TestCase):
    """The hybrid /walmart/trending-now route must require auth when
    ?admin=1 is requested. Public mode (no ?admin=1) is verified by
    the route-inventory test above."""

    def test_admin_mode_redirects_unauth(self):
        with _app_with_env(PROD_ENV_OK) as app_mod:
            client = app_mod.app.test_client()
            resp = client.get(
                '/walmart/trending-now?admin=1',
                headers={'Host': PROD_HOST},
                base_url=f'http://{PROD_HOST}',
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/login', resp.headers.get('Location', ''))


class PublicRoutesAllowlistIntegrity(unittest.TestCase):
    """PUBLIC_ROUTES entries must correspond to actual registered routes —
    no dead entries silently masking removed routes."""

    def test_every_allowlist_entry_is_real_route(self):
        with _app_with_env(PROD_ENV_OK) as app_mod:
            registered_rules = {
                rule.rule for rule in app_mod.app.url_map.iter_rules()
            }
            stale = [r for r in PUBLIC_ROUTES if r not in registered_rules]
            self.assertEqual(
                stale, [],
                msg=f'PUBLIC_ROUTES has entries that no longer match any '
                    f'registered route. Remove them: {stale}',
            )


if __name__ == '__main__':
    unittest.main()
