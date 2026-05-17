# Phase 0.3 — Team-Member Sessions

## Context

Echo-Dashboard authenticates admins today with a single shared password (`ADMIN_PASSWORD`, default `dan`) constant-time-compared in `_admin_session_check_password`; the session cookie carries only `session['admin_authed'] = True`. P0.3 introduces per-user identity (Dan / Steph / Laine) so writes to `collages`, `posts`, and `collection_content_drafts` are attributable, while keeping a single workspace (Phase 5 owns tenant split). Per the 2026-05-17 realignment, the `users` table is **shared upstream framework**; each deploy seeds its own team rows. Sequencing: PR #38 lands first (uniform decorators, `_admin_config_missing`, no URL-token page guards), then this work, then cherry-pick downstream.

## Approach

**Schema — `users` (shared upstream, in `db_schema.init_schema()`).** `creator_id` is in from day one so Phase 5 is a no-op for this table.

```sql
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,           -- use existing _PK macro
    creator_id    TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'editor',  -- 'owner'|'editor'|'viewer'
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_creator_name_ci
    ON users(creator_id, LOWER(name));
CREATE INDEX IF NOT EXISTS idx_users_creator_active
    ON users(creator_id, is_active);
```

Login is case-insensitive on `name` (matches existing password posture), hence the `LOWER(name)` unique index.

**Attribution columns** (use `_add_column_if_missing` — never `ALTER ... ADD COLUMN IF NOT EXISTS`, SQLite can't parse it):

```python
for tbl in ('collages', 'posts', 'collection_content_drafts'):
    _add_column_if_missing(conn, tbl, "created_by_user_id INTEGER")
    _add_column_if_missing(conn, tbl, "updated_by_user_id INTEGER")
    _add_column_if_missing(conn, tbl, "updated_at TIMESTAMP")  # collages lacks it
```

No FK on attribution columns — soft reference. A deactivated user must not cascade-delete historical rows; `is_active = FALSE` is the deactivation path. Render layer left-joins `users` and falls back to "Unknown" when NULL or row missing.

**Password storage.** `werkzeug.security.generate_password_hash` / `check_password_hash` (default `scrypt`, Werkzeug ≥ 2.3). Already in the Flask dep tree — zero new deps, zero native-build risk on Replit. `bcrypt`/`argon2` are stronger but require native builds; not worth the friction at three users.

**Seed mechanism.** `db_schema.seed_team_users(creator_id, spec)` — `spec` from a `TEAM_USERS_JSON` Replit Secret (JSON array of `{name, password, role}`). Pattern: `INSERT INTO users (creator_id, name, password_hash, role) VALUES (...) ON CONFLICT (creator_id, LOWER(name)) DO UPDATE SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, is_active = TRUE`. Idempotent on every boot; rotating a password = Secret edit + restart. Echo-Dashboard seeds its demo trio against `everydaywithsteph`; Shop-MomandMe seeds Dan/Steph/Laine against the same creator id (its only row). No hardcoded names in shared code.

**Session schema.** Replace `session['admin_authed'] = True` with `session['user_id']`, `session['user_name']`, `session['user_role']`. `_admin_session_authed()` checks `session.get('user_id')`. New `_current_user()` returns `{id, name, role}` (None when unauthed) for write paths to stamp attribution. All `_require_admin_page` / `@require_admin_api` callsites keep working unchanged.

**Login flow.** `templates/admin_login.html` gains a `name` field above the password field. `POST /admin/login` resolves `name` against `users WHERE creator_id = <active> AND LOWER(name) = LOWER(?) AND is_active`, calls `check_password_hash`, sets the three session keys. Active `creator_id` resolved via the P0.7 subdomain/env path; on both Echo-Dashboard demo and Shop-MomandMe today it's the single configured creator.

**`ADMIN_PASSWORD` deprecation.** New env flag `ADMIN_AUTH_MODE` ∈ {`team`, `legacy_single`}. Default: `team` if ≥ 1 active user exists for the active creator, else `legacy_single`. Legacy code path retained behind that flag for one full sync cycle. `_admin_config_missing()` (PR #38) keeps fail-closing on missing `SECRET_KEY`; missing `ADMIN_PASSWORD` only fails closed when `ADMIN_AUTH_MODE=legacy_single`. Removal of the legacy path is its own follow-up PR.

**Rollback.** If seeding fails or the new form errors in production: set `ADMIN_AUTH_MODE=legacy_single` in Replit Secrets and restart (no code redeploy). The legacy path runs against the existing `ADMIN_PASSWORD` Secret. `_admin_session_authed()` accepts either cookie shape during transition (`session.get('user_id') or session.get('admin_authed')`).

**Backfill.** Existing rows get `created_by_user_id = NULL`, `updated_by_user_id = NULL` (default from `ADD COLUMN`). Render shows "Last edited by Unknown · <timestamp>" for NULL or deactivated users. **Not** backfilling to "Dan" — fabricating attribution is worse than admitting unknown.

**"Last edited by" rendering.** Template helper `attribution_label(updated_by_user_id, updated_at, users_by_id)` returns the rendered string. Wired into `templates/walmart_collection_edit.html` and the posts-manage template. Single helper, consistent format.

## Files affected

- `db_schema.py` — **shared (upstream)**: `users` table, attribution columns on three tables, `seed_team_users()`.
- `app.py` — **shared (upstream)**: session keys, `_admin_session_authed()`, `admin_login()`, `_current_user()`, `ADMIN_AUTH_MODE` branching, write paths stamp `created_by_user_id` / `updated_by_user_id`.
- `templates/admin_login.html` — **shared (upstream)**: add `name` field.
- `templates/walmart_collection_edit.html` plus posts-manage template — **shared (upstream)**: render attribution.
- `tests/test_team_member_sessions.py` — **shared (upstream)**: new suite covering the cases below.
- `docs/PG_LAUNCH_RUNBOOK.md` — **shared (upstream)**: document `ADMIN_AUTH_MODE`, `TEAM_USERS_JSON`, rollback recipe.
- Per-deploy `TEAM_USERS_JSON` Replit Secret — **client-only** (Echo-Dashboard demo trio; Shop-MomandMe Dan/Steph/Laine).

## Verification

1. **Schema migration.** Fresh PG: `init_schema()` → `\d users` shows the table; `\d collages` shows `created_by_user_id`, `updated_by_user_id`. Re-run `init_schema()` — no errors (idempotent). Re-run against an already-migrated DB — same.
2. **Login smoke per user.** With three seeded users: `POST /admin/login` `name=Dan&password=…` → 302 to `/hub`, session has `user_id=<Dan.id>`. Repeat Steph, Laine. Wrong password → re-render with error. Missing name → same.
3. **Attribution write-then-read.** Login as Steph, create a collage → row has `created_by_user_id = updated_by_user_id = <Steph.id>`. Login as Laine, edit → `created_by_user_id` unchanged, `updated_by_user_id = <Laine.id>`. Edit page renders "Last edited by Laine".
4. **Fallback-to-single-password.** `ADMIN_AUTH_MODE=legacy_single`, restart, login with just `password=dan` → success. Old session cookie shape still authorized. New writes stamp `created_by_user_id = NULL` → "Unknown" renders.
5. **Historical rows.** Pre-P0.3 rows render "Last edited by Unknown · <timestamp>" — not a crash, not "by None".
6. **Suite.** `python3 -m unittest discover -s tests` stays green (target ≥ 301 + new cases).

## Open questions

1. **Role enforcement scope.** P0.3 stores `role` but doesn't yet differentiate `editor`/`viewer` in route guards. Confirm role-based authorization lands in P0.5 (auth hardening), not here.
2. **Initial passwords.** Who chooses Dan/Steph/Laine's seed passwords for Shop-MomandMe — Kelly, or first-login forced rotation? Default proposal: Kelly seeds via `TEAM_USERS_JSON`, users rotate via runbook procedure (re-issue Secret + restart).
3. **Internal chat + per-user AI logs** (feature-list §"Team Member Sessions" line 25). Out of scope for P0.3 schema; chat tables will need `from_user_id`/`to_user_id` columns in a later phase. Confirm.
