# Phase 0.5 — Admin Auth Hardening

## Context

PR #38 shipped the Codex-audit follow-ups (URL-token removal, seed-token deletion, `/archer/*` guards, `_admin_config_missing()` fail-closed, boot warning). What still ships is a signed-cookie session of `{'admin_authed': True}`, no CSRF on state-changing routes, no regenerate-on-login, no rotation policy, no audit trail. P0.5 closes those gaps before the framework lands on `admin.echotribe.ai` and Shop-MomandMe.

## Approach

### Session-flow audit findings
- **Shape:** `session['admin_authed']=True` (`app.py:1853`), read at `_admin_session_authed()` (`app.py:1772`), cleared via `session.pop`. Post-P0.3: `{'user_id','user_name','user_role'}`; helpers must accept either during transition.
- **Signing:** `itsdangerous.URLSafeTimedSerializer` over `SECRET_KEY`. No server-side store, no revoke list.
- **Lifetime:** `permanent_session_lifetime = 30 days` (`app.py:76`), `session.permanent = True` at login. No idle timeout.
- **Cookie attributes:** Flask defaults — `HttpOnly=True`, `Secure=False`, `SameSite=None`. Two are wrong for prod.
- **Reads/writes:** written in `admin_login`/`admin_logout`; read in the four auth helpers. No other module touches `session`.

**Hardening:** `SESSION_COOKIE_SECURE=True` gated on `_is_production_env()`, `SESSION_COOKIE_SAMESITE='Lax'` (Strict breaks `next=` bounces), `SESSION_COOKIE_NAME='echo_admin'`. 12-hour idle timeout via `session['_last_seen']` checked in a `before_request` hook on `/admin/*` and `/archer/*`; stale → `session.clear()` + redirect. Keep 30-day absolute lifetime as ceiling.

### CSRF posture
- **Library:** `Flask-WTF` (pulls only `itsdangerous`/`WTForms`; no native build). `CSRFProtect(app)` global; exempt `/archer/track_click` and routes guarded solely by `X-Walmart-Trends-Admin-Token` (cron — header presence ≠ browser).
- **Form routes** (`/admin/login`, draft publish/unpublish/archive, posts manage): hidden `{{ csrf_token() }}`, validated by Flask-WTF.
- **JSON API routes** under `@require_admin_api` (all `/archer/collage/*`, `/archer/posts/*`, `/archer/campaigns/*`, `/api/walmart/collections/*`, `/api/collection-content-drafts/*`, `/admin/walmart-trends/*`, `/admin/amazon-trends/enrich`): require `X-CSRF-Token` matching a per-login session-bound token (template `<meta name="csrf-token">`). `require_admin_api` compares after the auth check; header-token (cron) callers bypass — no cookie. Tokens rotate per login; `hmac.compare_digest`.

### Session-fixation defense
Signed-cookie sessions have no server-side ID to rotate. Equivalent: at the top of the successful login branch call `session.clear()` *before* setting new keys, then write `session['user_id']`, `session['_csrf']=secrets.token_urlsafe(32)`, `session['_login_at']=int(time.time())`, `session.permanent=True`. On logout, `session.clear()`. On privilege change (P0.3 role flip), bump a per-user `min_login_at` in a `user_session_floor` table; cookies with `_login_at < floor` fail auth next request.

### Password rotation policy (P0.3 team users)
- **Storage:** P0.3 specifies `users.password_hash` via `werkzeug.security` scrypt. Add `password_set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`, `must_change BOOLEAN NOT NULL DEFAULT FALSE`, `last_used_hashes TEXT[] NOT NULL DEFAULT '{}'` (last 5).
- **Complexity:** ≥ 12 chars, ≥ 1 letter + 1 digit, not equal to `name` (CI). `validate_password()` shared between seeding and self-service.
- **Cadence:** 180-day age cap. After expiry, login succeeds but redirects to `/admin/password/change`; `must_change=TRUE` short-circuits the same flow on first login after a `TEAM_USERS_JSON` reseed.
- **Mechanism:** self-service `/admin/password/change`. Ops rotation = Kelly edits `TEAM_USERS_JSON` Secret + restart; seeder sets `must_change=TRUE` on any hash change.
- **Forced triggers:** suspected exposure (audit event), role downgrade, departure. **History:** 5-deep; new hash compared against all five.

### Audit log
Shared upstream table; all writes through `audit.record(event, user, target, before, after)`.

```sql
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    creator_id  TEXT REFERENCES creators(id) ON DELETE SET NULL,
    user_id     INTEGER,           -- soft ref; NULL on failed login
    user_name   TEXT,              -- denormalized for failure/delete
    event       TEXT NOT NULL,     -- login.ok|login.fail|password.change|collection.publish|...
    target_kind TEXT,
    target_id   TEXT,
    ip          INET,
    user_agent  TEXT,
    before      JSONB,
    after       JSONB,
    request_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_created    ON admin_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user_event ON admin_audit_log(user_id, event, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target     ON admin_audit_log(target_kind, target_id);
```

**Write hooks:** `admin_login` (ok/fail), `admin_logout`, `/admin/password/change`, every `@require_admin_api` write via a decorator wrapping the existing one (captures `request.json`/`form` after-image). Mirror to structured `logging.info({...})` so Replit logs catch events if PG is degraded. **Retention:** 365 days, pruned nightly by `audit.prune()` from the existing scheduled deploy; write events older than 90 days collapse to one row per `(user, event, day)`. **Read access:** `/admin/audit`, owner role only (P0.3 `role`), paginated/filterable. No raw DB access from templates.

## Files affected

- `app.py` — **shared (upstream)**: `CSRFProtect` init, cookie attributes, idle-timeout hook, `session.clear()` on login, `_login_at` floor check, `/admin/password/change`, `/admin/audit`, audit-writer wrapping `require_admin_api`.
- `db_schema.py` — **shared (upstream)**: `admin_audit_log` + indexes; `users` columns `password_set_at`, `must_change`, `last_used_hashes`; `user_session_floor` table; `prune_audit_log()`.
- `audit.py` *(new)* — **shared**: `record()`, `prune()`, `query()`.
- `auth.py` *(new)* — **shared**: `validate_password()`, `password_reuse_check()`, CSRF helpers.
- `templates/admin_login.html`, new `admin_password_change.html`, new `admin_audit.html`, every admin form partial — **shared**: `{{ csrf_token() }}`.
- `static/js/admin_csrf.js` *(new)* — **shared**: attach `X-CSRF-Token` to admin `fetch()`.
- `tests/test_admin_auth_hardening.py` *(new)* — **shared**.
- `docs/PG_LAUNCH_RUNBOOK.md` — **shared**: rotation procedure, retention, CSRF notes.
- `ADMIN_SESSION_IDLE_SECONDS` Replit Secret — **client-only** override.

## Verification

1. **CSRF rejection.** `POST /archer/collage/save` with valid session but no/stale `X-CSRF-Token` → 400; current → 200. Form POST without hidden token → 400.
2. **Session fixation.** Capture cookie pre-login; POST `/admin/login`; payload changes (cleared keys + new `_csrf` + `_login_at`). Replay of pre-login cookie still fails auth.
3. **Idle timeout.** Authed; sleep > `ADMIN_SESSION_IDLE_SECONDS` (test=2s); next admin request → redirect; `/healthz` unaffected.
4. **Cookie attributes.** Under prod env: `Set-Cookie: ... Secure; HttpOnly; SameSite=Lax; Path=/`.
5. **Password policy.** `/admin/password/change` rejects < 12 chars, no digit, equals username, equals any of last five hashes; accepts a fresh value; old hash rejected on next login.
6. **Forced rotation.** Seeder sets `must_change=TRUE`; login redirects to `/admin/password/change` and refuses other navigation until completed.
7. **Audit smoke.** Login ok / bad password / publish-draft each yield correctly-shaped rows. Owner sees `/admin/audit`; editor → 403.
8. **Cron unaffected.** `POST /admin/walmart-trends/refresh` with `X-Walmart-Trends-Admin-Token`, no cookie → 200, no CSRF, audit row records `user_name='cron'`.
9. **Suite.** `python3 -m unittest discover -s tests` stays green; target ≥ 301 plus new cases.

## Open questions

1. **Retention vs legal hold.** 365-day default — confirm FTC/GDPR posture for login-failure events.
2. **`/admin/audit` scope.** Owner-only proposed; confirm Steph (likely `editor`) does not need read access to her own attribution history.
3. **Rotation cadence.** 180 days proposed; NIST SP 800-63B argues against forced rotation. Default stays unless Kelly opts out — the column + flag stay either way.
4. **Cron identity.** `user_name='cron'` is a placeholder; if multiple cron jobs land, split `WALMART_TRENDS_ADMIN_TOKEN` into per-job tokens with distinct labels.
5. **Idle-timeout default.** 12 hours covers a workday; 8 hours is conservative. Confirm with the three team users.
