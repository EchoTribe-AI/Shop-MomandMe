# Phase 0 Implementation Plans

Per-sub-phase implementation plans for the EchoTribe / Shop-MomandMe initiative. Each doc is the spec that engineering executes against for that sub-phase.

These files are **shared upstream** (Echo-Dashboard owns the canonical copy; Shop-MomandMe syncs them down via the cherry-pick ritual documented in `Shop-MomandMe/docs/UPSTREAM_SYNC.md`).

## Index

| # | Sub-phase | Owner agent | File |
|---|---|---|---|
| P0.1 | URL Re-Architecture (`/archer/*` → `/admin/*` / `/api/*` / `/shop/*`) | Software Architect | [PHASE0_01_url_rearchitecture.md](PHASE0_01_url_rearchitecture.md) |
| P0.2 | Insights Rebuild (Creator-Facing Framework) | UX Architect | [PHASE0_02_insights_rebuild.md](PHASE0_02_insights_rebuild.md) |
| P0.3 | Team-Member Sessions (Dan / Steph / Laine) | Backend Architect | [PHASE0_03_team_member_sessions.md](PHASE0_03_team_member_sessions.md) |
| P0.4 | click_log Redesign + Event Contract | Database Optimizer | [PHASE0_04_click_log_redesign.md](PHASE0_04_click_log_redesign.md) |
| P0.5 | Admin Auth Hardening (CSRF, session fixation, rotation, audit log) | Security Engineer | [PHASE0_05_admin_auth_hardening.md](PHASE0_05_admin_auth_hardening.md) |
| P0.6 | Slim-Down (Beyond Action 1) | Software Architect | [PHASE0_06_slimdown.md](PHASE0_06_slimdown.md) |
| P0.7 | Storefront Framework Boundary (per-creator schema + branding/) | Software Architect | [PHASE0_07_storefront_framework_boundary.md](PHASE0_07_storefront_framework_boundary.md) |

## Reading order

Read **P0.7** first if you're new to the initiative — it defines the active-creator resolution that P0.1 / P0.2 / P0.3 / P0.4 all depend on.

Read **P0.4** before P0.2 — the insights rebuild reads from the redesigned click_log schema.

Read **P0.1** before P0.6 — slim-down references routes that P0.1 renames.

## Plan format

Each plan follows a fixed structure (the brief enforces this so the docs are scannable):
- **Title** with sub-phase ID
- **Context** — one paragraph: what the feature list asks for, what runbook constraints apply
- **Approach** — the recommended implementation
- **Files affected** — absolute paths, each marked shared (upstream) or client-only
- **Verification** — how to confirm the change works end-to-end
- **Open questions** — items that block execution, with owner per item

No timeline labels (week / day / Friday) anywhere. Phases run to completion at their own pace; a recurring retrospective reviews what landed.

## Cross-cutting context

- **Two-repo architecture**: Echo-Dashboard upstream (EchoTribe ops + framework), Shop-MomandMe downstream (per-creator deploy). The high-level roadmap doc (`ARCHITECTURE_ROADMAP.md`) lives in a private planning workspace outside the repo; ask the project owner for the current copy if you need it.
- **2026-05-17 realignment**: the storefront — templates, `/shop/`, `/collections/`, `/admin/login`, partials — is **shared upstream framework**, not client-only. Per-creator differences live in the `creators` DB row plus a per-deploy `branding/` directory. P0.7 owns the mechanism.
- **PR #38 (Echo-Dashboard)** is the open Action 0 hardening PR; several plans treat its decorators (`@require_admin_api`, `@require_admin_page`) and `_admin_config_missing` as merged inputs. Sync downstream after merge.
- **Action 1 (revised)** strips EchoTribe-internal `/archer/*` ad-ops surface from Shop-MomandMe; storefront framework stays. Several plans coordinate with Action 1's deletions.
