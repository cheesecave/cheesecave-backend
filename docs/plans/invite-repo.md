# Invitation repositories implementation record

Status: active. Overall feature is **not complete**. Last updated: 2026-11-10 (Asia/Hong_Kong).

## Scope and invariants

- User-visible visibility: public, private, invite. Retain the HF boolean `private`; invite is an additional `invite_mode` flag and requires private=true, enforced in SQL.
- HF fields, protocol routes, downloads and masked RepoNotFound denial remain compatible. Invite is not HF gated; gated APIs remain unsupported.
- Invite URLs expose only default-branch root `INVITE.MD` and an application form to unapproved viewers. Missing INVITE.MD uses a default page. Never expose README, siblings/history/raw addresses before access has been checked. Relative links/images do not make other private files public. Invite does not enter public discovery.
- Signed-in applications need manager review; approval grants that account read/download access to this repository. Anonymous applications require email; managers may issue a repository code after approval.
- Repository codes work for registration, existing-account binding, or an isolated temporary browser guest session. Account grants are read-only, repository-specific, independent of organization membership, and use the account's own normal access-token for HF.
- Codes are hashed, expiring, limited, revocable and audited. Account redemption is idempotent; account creation, redemption accounting and grants are atomic and concurrency-safe. Open registration still processes a supplied repository code. Repository codes must not bypass a disabled registration policy.
- Separate lifetimes for code redemption, guest sessions and account grants. Revoking a code revokes derived guest sessions, but account grants are revoked explicitly and independently. Account redemption quotas and guest session quotas are separate.
- Guests can use only explicitly controlled bounded preview APIs for that repository. Guest identity never enters normal user/token permissions or replaces login. No raw/Range download, presigned URLs, Git/LFS/Xet access, account management or token creation. Product promise: no original-file/bulk download; displayed material can still be copied.
- Excluded: temporary accounts; HF access-token issuance/redemption; using repository codes as HF tokens; an anonymous-download option. No merge, deployment, unrelated cleanup or ordinary HF token refactor.

## Verified baselines and workspaces

The original worktrees remain untouched on `feat/manual-ssh-deployment`. They contain unrelated deployment files/workflows and admin logo edits. All task work uses the following isolated worktrees, on the same branch **feat/invite-repo** in each independent repository. Reuse these on continuation; never recreate/reset the branch.

| Repository | Worktree (relative to CheeseCave) | Verified origin default baseline | Phase commits |
| --- | --- | --- | --- |
| backend | `.worktrees/invite-backend` | `def65a7c11bf7dca4e913fa47b3fb5e48e5279ea` | pending |
| web | `.worktrees/invite-web` | `03ca1166d9b17e8b5f85132fa8f6eb2396a16073` | pending |
| admin | `.worktrees/invite-admin` | `825048e74779361d5b5f71f46520920a52fdaf24` | pending |

Default `main` verified using `git ls-remote --symref origin HEAD`, then `git fetch origin main`. Applicable root AGENTS.md read in all three task worktrees. Branch creation started from origin/main, not the stale local main branches. No task branch existed beforehand. Parent directory is not managed as a Git repository.

## Executable phases and acceptance

1. **Complete: backend state and grants foundation.** Migration 043, `Repository.invite_mode`/visibility and `RepositoryAccessGrant`; shared active-grant predicate used by detail/list/feed; owner-FK repository manager permission; create/settings conversions; preserve HF bool/gated behavior. Admin API returns invitation mode and product visibility separately. Tests cover state invariants, alias/SQL parity, expiry/revocation, isolation, no write escalation and migration reruns. Web controls wait for the access preflight contract in phase 3; administrator display is being committed separately after its full checks.
2. **Pending: application and code lifecycle.** Separate application, invitation, redemption/audit and guest models. Manager-only list/review/issue/revoke/grant administration; account approval; anonymous approved applications; transactional registration and binding including policy, idempotency, limits, expiry/revocation and concurrency tests.
3. **Pending: invitation page and frontend flows.** Access-state endpoint and bounded INVITE.MD read; preflight gate for root/tree/blob/commits/settings/edit/upload; safe Markdown; create/settings visibility, application status/review, issuance and account binding. UI tests must prove no ordinary content requests/mounting before preflight.
4. **Pending: isolated guest browsing and previews.** Hashed independent expiring/revocable repo-scoped guest cookie/session; dedicated bounded tree/metadata/text/thumbnail/limited-row interfaces; no raw URL/Range offsets. Audit all raw/Git/LFS/Xet/fallback paths and mixed normal-login/guest behavior. Record supported preview types and disabled PDF/video/archive/model inference boundaries.
5. **Pending: integration and documentation.** Targeted and broad backend regressions; repository HF client version matrix; web/admin tests and production builds; actual browser flows; English API/feature documentation; review diffs and commit only task files.

Every completed phase gets a small complete commit and an updated record. Backend owns the cross-repository contract; dependent UI commits record backend minimum commit. Never describe partial work as complete.

## Design decisions

- Use separate models for requests, account grants, codes and guest sessions. Guest permission is recognized only at controlled browser endpoints, never in `get_optional_user` or the shared normal read predicate.
- For HF settings, `private=true` / `visibility=private` preserves existing invite mode. An explicit request to public clears invite mode; enabling invite mode requires private=true (or an otherwise consistent invite visibility). Reject conflicting inputs without writes. `gated` retains its current refusal behavior. Exact conversion tests accompany implementation.
- Current default branch is hardcoded main throughout existing repository code; invitation content follows that actual repository default rather than accepting an arbitrary caller ref/path.
- Existing main already centralizes normal list/detail access in `auth/permissions.py`. Existing LFS repository scoping and fallback denial propagation are implemented and must be regression-tested, not rewritten from stale risk hints.
- Site policy currently has `invitation_only` and no separate registration-disabled setting. Repository codes grant repository access **in addition to**, never instead of, site registration authorization. In open mode they can be supplied alone; in invitation-only mode a valid site `register_account` invitation is also required. Add a separate repo-code input so both can be submitted together. Do not alter ordinary HF token issuance.

## Current findings

- Read policy currently allows public, owner FK and current organization members, without a single-repository grant.
- LFS `_authorize_batch` already rejects missing/unreadable repos; `process_download_object` checks repository object holding and tombstones. Git central read authorizer uses the shared permission helper. Fallback explicitly rethrows RepoReadDeniedError.
- Current dataset previews run in the browser over raw resolve; there is no existing backend dataset viewer to reuse for restricted guests.
- Web `repo-view-routes.js` maps root/tree/commits/blob/commit into RepoViewer. RepoViewer currently starts info/tab loads concurrently and mounts detail children early. A gate around the routed repository component must precede mounting, including separate settings/edit/upload routes and spaces.
- MarkdownViewer alone is insufficient for public invitation Markdown relative resource isolation.

## Validation log

- Git inspection: status/branches/remotes/worktrees/log/default HEAD and fetched baseline, all three repos: passed. Original unrelated modifications preserved.
- Host tools: Node 22.21.1, pnpm 10.18.1, uv available. Default Python lacked peewee. A task-local ignored `.venv` is being provisioned with `uv pip install --python .venv/Scripts/python.exe -e '.[dev]'`.
- Full integration, HF client matrix and browser flows: pending later phases; the checks below validate only the first slice.
- Phase 1 targeted PostgreSQL run (dedicated database `cheesecave_invite_tests` on existing local test PostgreSQL, fresh schemas): **180 passed in 225.16 s**. Files: new invite migration/grants/visibility tests; existing permission/visibility/settings/CRUD tests and migrations 040/041/042. `db_dual` ran both SQLite and PostgreSQL migration cases. Follow-up for final admin serialization and settings-manager wiring is recorded after completion.
- SQLite visibility + existing settings + CRUD: **69 passed** before the final two checks; final visibility/settings subset **28 passed**. New visibility resolver: **100% statement and branch coverage** (32 grant/visibility tests at that point). Mock ratchet before/after: **43** (17 ORM attribute, 26 fake query, 0 mocked database calls).
- Test bootstrap adjustment: `uv sync --locked --extra dev` refuses the existing lock/manifest mismatch. `uv sync --frozen --extra dev` installs pinned runtime packages; the lock omits the manifest's dev `jsonschema`, so install that locally with `uv pip install --python .venv/Scripts/python.exe jsonschema`. No dependency/lock file modified. First broad fast collection failed for this missing dependency and an unchanged UTF-8 fixture read under Windows cp936. Set `PYTHONUTF8=1` for all subsequent runs; a rerun is in progress. These are environment/baseline collection failures, not feature assertions.
- Both frontends installed with `pnpm install --frozen-lockfile`; admin focused three-state display tests **12 passed**, production build passed, full coverage tests pending. No merge/push/deploy performed.
- Final admin serialization + real settings-manager wiring tests: **2 passed on SQLite**. PostgreSQL follow-up was interrupted when Docker Desktop became unavailable (`Docker Desktop is unable to start`); it produced no assertion result. The earlier 180-test PostgreSQL run completed successfully before this outage. Recheck Docker and rerun these two checks with phase 2 integration; do not restart unrelated services or claim they passed on PostgreSQL.

## Resume procedure / next concrete operations

1. Read this file first. Run `git status --short`, `git branch --show-current`, `git log -6 --oneline` in all three recorded task worktrees; compare actual phase commits with this record. Inspect original worktrees only to ensure unrelated changes remain preserved.
2. Continue phase 2 and finish any pending broad/admin check results. Use local `.venv/Scripts/python.exe`, set CHEESE_CAVE_DB_BACKEND=sqlite, CHEESE_CAVE_DATABASE_URL=sqlite:///:memory:, PYTHONPATH=src;. (Windows), PYTHONUTF8=1; use real shared database fixtures. Recheck Docker availability before PostgreSQL checks on 127.0.0.1:25432; use only dedicated test database `cheesecave_invite_tests` and fixture-isolated schemas. Do not use production data.
3. Continue phase 2 using these same branches, documenting exact API contracts for phase 3 before editing dependent frontends. Do not restart design or create per-session branches.
