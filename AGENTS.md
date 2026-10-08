# CheeseCave Backend: agent guide

CheeseCave is a self-hosted model and dataset repository service. This repository holds the
FastAPI API, Git/LFS endpoints, background workers, storage code, migrations and the shared
Compose configuration. It is an independent fork of KohakuHub; keep the upstream attribution
in `NOTICE.md`, `LICENSE` and `provenance/` intact when editing inherited code.

## Maintenance discipline

- **Branches and pull requests.** Do not commit to `main`. Work on a feature branch, open a
  pull request, and wait for its checks. Do not merge, deploy or publish without explicit
  authorization from the maintainer; approval for one change does not cover the next one.
- **Commits.** Write English subjects in the imperative mood (`fix: ...`, `ci: ...`). Stage only
  the files you intend to change. Before committing, check `git status` for bytecode
  (`__pycache__/`, `*.pyc`), coverage output, `lint-reports/`, `hub-meta/`, `hub-storage/`, `.env`
  files and local virtualenvs. These are ignored; a force-add or a missing ignore rule is a bug.
- **Secrets.** Never commit tokens, keys or passwords, and never print their values in logs or
  transcripts. CI credentials live in repository secrets. Example files use `CHANGE_ME` or
  generated values only.
- **Tests first.** For behaviour changes, write the failing test first, then the code. Cover every
  changed line of runtime code. Do not delete, skip or weaken assertions to make a run pass; if a
  test is wrong, fix it and say why in the pull request.
- **Test scope.** The unit-test workflow runs only when code, tests, scripts, Compose or build
  configuration changes. Documentation, images and other resources must not start the full matrix.
  Coverage measures the runtime code in `src/kohakuhub/` (including its `utils/` modules when
  the application imports them). It excludes tests, `scripts/` and tooling, migrations, legacy
  modules and documentation. See [CONTRIBUTING.md](CONTRIBUTING.md#test-and-coverage-scope).
- **Compatibility.** Keep the Python package name `kohakuhub`, protocol identities, storage
  identifiers and client paths. Settings are read from `CHEESE_CAVE_<NAME>` first and fall back to
  `KOHAKU_HUB_<NAME>` (see `read_env` in `kohakuhub.config`); never remove the fallback. Renames of user-facing product
  text use CheeseCave; KohakuHub appears only as attribution.
- **Deployment.** Production deployment is a manual workflow on `main`. Do not change deployment
  targets, hosts or secrets from code.
- **Documentation.** Keep English as the source of repository documents. Where a Chinese
  companion exists (`*.zh-CN.md`), update both in the same change.

## Test database discipline

- **Real SQL by default.** A test that checks what the database decides (queries, filters,
  ordering, counts, uniqueness, foreign keys, transactions, what gets written) runs real SQL.
  Do not replace the ORM or query objects with fakes for those checks.
- **Use the shared fixtures.** They live in `test/conftest.py` and `test/kohakuhub/support/db.py`:
  `db_fresh` for code that commits over HTTP, migrations and DDL (a new file-backed database per
  test); `db_committed` for rows that must be really committed but may be emptied per test;
  `db_scope` (rolled back after each test, same thread only) for query-shaped tests. Use
  `table_missing(database, Model)` for a real database failure. Seed rows with
  `test/kohakuhub/support/factories.py`. Do not construct `SqliteDatabase` or `PostgresqlDatabase`
  in a test file; only `support/db.py` does.
- **Keep the mocks that belong to the environment.** External services (LakeFS, S3, Hugging
  Face HTTP, SMTP) stay mocked; their database side uses real rows. Pure logic that never reads
  the database may stay a plain unit test without fake ORM objects. A deliberate database
  failure (outage, injected constraint error) may keep a targeted mock; say why in a comment.
- **Ratchet (discipline, not CI).** The count from `python scripts/dev/mock_ratchet.py` may go
  down and must not go up. Baseline when the migration started (2026-10-09): **291** (67 ORM
  attribute monkeypatches, 224 fake query or field references, 0 mocked database calls). A PR
  that touches tests states the count before and after in its description. Reviewers reject a
  PR that raises the count without a stated reason in the description and a linked issue.
- **Coverage must not drop.** A change keeps or raises the branch coverage of the lines it
  touches; new runtime code aims for 100 % branch coverage. The coverage number before and after
  goes in the PR description.
- **Migrations keep a fresh database.** Tests of schema history use `db_fresh`; never `db_scope`.

## Layout

- `src/kohakuhub/`: application package. `api/` holds routers, `auth/` permissions and tokens,
  `db.py` models, `worker.py` and `tasks.py` background jobs, `utils/` shared helpers.
- `test/kohakuhub/`: pytest suite: `api/` tests, top-level unit tests and `support/` fixtures.
- `scripts/`: operational and CI tooling. `scripts/ci/compose.yml` is the isolated test stack.
- `docker/`, `compose.yml`, `compose.build.yml`, `docker-compose.example.yml`: deployment files.
- `docs/`: user and operator documentation, with `*.zh-CN.md` companions where they exist.

## Commands

```sh
pip install -e ".[dev]"            # backend and test dependencies
make test-backend RANGE_DIR=...    # one area of the suite; full suite needs PostgreSQL, MinIO, LakeFS, Valkey
KOHAKU_HUB_DB_BACKEND=sqlite KOHAKU_HUB_DATABASE_URL="sqlite:///:memory:" PYTHONPATH=src:. \
  python -m pytest test/kohakuhub -p scripts.ci.select_tests --ci-category fast -q
```

## Code conventions

These follow the inherited [CONTRIBUTING.md](CONTRIBUTING.md) and apply to new code.

- Python 3.10+. Use native generics and unions (`list[str]`, `X | None`) and `match` where it
  clarifies. Do not import from `typing` for these.
- Group imports: standard library, third-party, then `kohakuhub`. Put imports at the top of the module.
- Database access is synchronous Peewee. Wrap multi-step writes in `with db.atomic():`; plain
  reads need no transaction.
- Check permissions before any write, using the helpers in `kohakuhub.auth.permissions`.
- Errors use the Hugging Face-compatible shape, for example
  `HTTPException(status_code=404, detail={"error": "..."}, headers={"X-Error-Code": "RepoNotFound"})`.
- Background task handlers (`@task` in `kohakuhub.tasks`) must be re-runnable. Keep intermediate
  artifacts under `ctx.scratch_prefix`, publish results in one step guarded by `ctx.assert_owned()`,
  and test interruptions with `kohakuhub.task_testing`.
- Keep the `kohakuhub` import path and route prefixes stable; add new routes beside existing ones.
