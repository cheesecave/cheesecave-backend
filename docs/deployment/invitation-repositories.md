# Invitation repositories: review and release

This release spans `cheesecave-backend`, `cheesecave-web` and `cheesecave-admin`,
each on `feat/invite-repo`. The Backend provides the access APIs and migrations;
Web depends on them even when opening an ordinary repository. Admin adds the
three-state visibility display.

## Review map

| Repository | Main changes | Review entry points |
| --- | --- | --- |
| Backend | Invite visibility, single-repository grants, applications, hashed codes, registration/redemption, guest previews, download permission checks | [Feature contract](../features/invitation-repositories.md), [authentication API](../api/authentication.md), [implementation and test record](../plans/invite-repo.md) |
| Web | Invitation landing, shared repository/preview layout, Settings tables, signup handoff and authenticated one-step redemption | [Repository access](https://github.com/cheesecave/cheesecave-web/blob/feat/invite-repo/docs/api/repository-access.md), [authentication API](https://github.com/cheesecave/cheesecave-web/blob/feat/invite-repo/docs/api/authentication.md) |
| Admin | Public, Private and Invite badges in the repository list/detail | `src/pages/repositories.vue` and its visibility tests |

## Current review status (2026-11-14)

The inspected runtime revisions are Backend `46ae96b` (documentation HEAD
`4b918ef`), Web `54a1017`, and Admin `a0ffbe3`. Documentation-only follow-ups do
not change those runtime revisions. The following work remains before release:

- Integrate Backend main `e01e330` and Web main `24871bd`. The current conflicts
  are Backend `test_info_unit.py` / `test_files_unit.py` and Web `RepoViewer.vue`
  / the repository blob page. Preserve main's UTC, large-directory and TAR fixes.
- Adapt the four feature-introduced date reads reported by main's Web UTC guard:
  three in `RepositoryAccessManager.vue` and one in `utils/repository-access.js`.
  Keep server timestamps separate from local date-input conversion.
- Publish rendered PDF page files atomically. `media_worker.py` currently writes
  the final PNG while `repository_media.py` can read it as soon as it exists.
  Add a concurrent-reader regression.
- Recover missing/expired signup handoffs. In open registration, a lost
  `sessionStorage` invitation can currently create an ordinary account and return
  it to a repository without granting access. Cover expiration and copied links.
- Satisfy the DCO workflow for the existing feature commits and run CI on the
  final integrated candidate. The reviewed 47 Backend, 32 Web and 1 Admin commits
  lack `Signed-off-by` trailers.

These are review findings, not completed fixes. The API and user guides now
describe the final code field, redemption page, invitation lifecycle and previews.

## Validation evidence

Counts below belong to their recorded revision and overlap; do not sum them.
Exact commands and results are in the [implementation record](../plans/invite-repo.md).

| Check | Recorded result |
| --- | --- |
| Latest Backend redemption change | 189 related cases; 29 endpoint cases on each of SQLite and PostgreSQL |
| Latest Web redemption change | 110 focused cases and 61 title cases; real browser login-return, redemption and repository navigation |
| Web production build at `54a1017` | Frozen-lockfile install and `pnpm build` passed; entry chunk about 1.31 MB / 409 KB gzip |
| Earlier complete Web run | 3638 passed; 96.46% lines/statements, 93.17% branches, 86.24% functions |
| Earlier Backend runs | SQLite fast: 1120 passed, 5 documented conditional skips; invitation selection: SQLite 571 and PostgreSQL 143 passed |
| Earlier HF matrix | Versions 0.20.3, 0.36.2, 1.6.0 and 2.4.0: 3 cases passed per version |
| Admin at `a0ffbe3` | 1315 cases in 76 files and production build passed |

Backend mock ratchet stayed at 43 in the recorded follow-ups. Full final-candidate
changed-line coverage and before/after comparisons still need CI evidence. Local
browser acceptance used real authentication and SQL with synthetic storage.

## Prepare the release candidate

1. Record the three reviewed commit SHAs and immutable image digests. Build from
   those revisions after the findings above are resolved; run the full suites,
   HF client matrix and changed-line coverage on that same candidate.
2. In staging, restore a production database copy and run the normal migration
   runner, `python scripts/run_migrations.py`, with staging configuration. The
   API image's startup runs this runner before serving. Existing databases need
   043 (invite state and account grants) and 044 (requests, codes, redemptions,
   guest sessions and audit). Fresh databases use `init_db()` through the runner.
3. Record migration duration, old public/private access, the constraint that
   Invite remains private, and repeat-start idempotency. Rehearse restoring the
   backup and verify the prior image against the upgraded schema before relying
   on an image-only rollback.
4. Use the actual Linux images, LakeFS, S3/R2 and HTTPS proxy. Verify installed
   PDFium/FFmpeg dependencies, their licenses, PDF worker MIME/CORS, guest-cookie
   scope/Secure behavior, and authenticated response caching.
5. Measure cold PDF/video processing, peak memory and scratch disk, simultaneous
   native decoder jobs and cleanup. Limits are per API process: the default four
   processes can run eight native jobs, and a video source can reach 256 MiB.
   Verify decoder isolation and anonymous request/session rate limits and retention.

## Cutover order

| Step | Action | Required check before the next step |
| --- | --- | --- |
| 1 | Record current versions/configuration and take a restorable database backup; coordinate writes for the migration window | Backup restore rehearsal and candidate acceptance recorded |
| 2 | Roll out Backend and compatible workers with migrations 043/044 | Health, migration completion, ordinary public/private reads and `/access` preflight pass |
| 3 | Roll out Web from the paired candidate; invalidate cached HTML as required by the deployment | Ordinary repositories load; anonymous Invite landing and login/signup/redemption return paths pass |
| 4 | Roll out Admin | Public/Private/Invite list and detail labels match Backend |
| 5 | Run the acceptance matrix below and monitor errors/resources | Record image digests, outcomes and any follow-up incidents |

Use the repositories' existing manual production workflow on `main` after PR
approval. Publish the Backend before Web; new Web against old Backend fails at
repository access preflight. Admin tolerates older responses by displaying
Private, but needs the new Backend to distinguish Invite.

## Acceptance matrix

| Flow | Expected outcome |
| --- | --- |
| Public / ordinary private repositories | Existing browser, HF and download contracts remain intact |
| Anonymous Invite root and direct tree/blob/commits links | Only public `INVITE.MD` or the default application page; no private linked resources |
| Account application approval | One repository grant; normal personal token can read/download only authorized repositories |
| Anonymous application approval | Manager can issue a code; no automatic account grant or email delivery |
| Site-code and repository-code registration | Site registration policy enforced; account/grant/code writes atomic; automatic repository return |
| Existing account `/repo-access` | Anonymous user goes to login; one code submission grants access and opens its repository |
| Repeat, concurrent, expired and revoked redemption | One account per code, idempotent same-account redemption, no orphan accounts or restored revoked grants |
| Guest plus ordinary login | Cookies coexist; independently authorized account gets normal access; guest scope stays on its repository |
| Guest file/card/history/metadata and PDF/HLS | Shared layout, controlled content, no original or presigned file URL |
| Raw GET/HEAD/Range, Git/LFS/Xet, datasets/TAR and fallback | Guest and cross-repository requests cannot bypass access checks |
| Revocation | Code revocation stops new exchanges and derived guest sessions; account grants are revoked separately |
| Existing signed downloads | Account revocation blocks new requests; previously signed URLs retain their own expiry |

## Rollback order

1. Roll Web back first so it stops depending on the new preflight and guest APIs.
   Roll Admin back if needed. Verify ordinary repository browsing.
2. If Backend remains healthy, retain it while investigating the frontend issue;
   this preserves the new grants and invitation records.
3. If Backend must roll back, use the image/schema combination proven in the
   rehearsal. The old Backend does not recognize single-repository grants, and
   its visibility writes need testing against the new constraint.
4. If database restoration is required, stop writes, retain the failed database
   for reconciliation, and restore the verified backup with its matching Backend
   and workers. Reconcile accounts, grants and other writes created after backup
   before reopening writes. Record the actual recovery point and data impact.

There is no verified automatic down-migration for this feature. Do not drop its
tables as a substitute for a tested restore. Recheck public/private access and
ordinary HF downloads after rollback.
