# Manual backend regression CI

English | [简体中文](ci.zh-CN.md)

The workflow is prepared for a future manual run. Repository Actions stays disabled
until the owner deliberately enables it; adding or pushing this configuration does
not dispatch a run. Its only event is `workflow_dispatch`: there are no push, pull
request, schedule, deployment, image publishing or third-party coverage upload jobs.

| Category | Coverage | Infrastructure |
| --- | --- | --- |
| `fast` | Existing unit suites and selected isolated SQLite/API regressions on Python 3.10 and 3.12 | None |
| `migrations` | Isolated SQLite/PostgreSQL schema, lossless upgrades, retries and migration ordering | PostgreSQL 15 |
| `services` | Complete backend suite, existing 80% coverage gate, cache enabled and disabled | PostgreSQL, MinIO, LakeFS 1.86.0, Valkey |
| `hf` | Existing real HF client suites and fallback interoperability, Python 3.12; client versions 0.20.3, 0.36.2, 1.6.0 and latest | Complete isolated stack |
| `all` | All the above jobs | Each job uses its own fresh runner and data |

The selector is opt-in. Normal pytest runs stay unchanged. `fast` excludes integration
parameters and tests using the full-stack fixtures. `migrations` excludes old
migration tests that bootstrap the complete seeded stack; `services` includes those.
It also skips unrelated test modules before import, so route imports cannot initialize
a fresh application schema while an old-schema migration is being tested.
A category with no selected tests fails. Tests may skip an unsupported client API,
a SQLite-only limitation or a deployment-specific configuration; the JUnit report
and log retain those reasons. These categories do not claim to certify every Python,
HF client, older LakeFS release or bucket-in-endpoint deployment combination.

The workflow is integrated on the default branch, `main`. A future manual run
still requires the owner to deliberately enable Actions. After doing so, choose **Backend regression
(manual only)**, a branch/ref and category in the GitHub Actions UI. This document
and configuration do not enable Actions or perform a dispatch.

## Commands and isolation

The fast command can also run locally after installing `.[dev]`:

```sh
KOHAKU_HUB_DB_BACKEND=sqlite KOHAKU_HUB_DATABASE_URL=sqlite:///:memory: PYTHONPATH=src:. python -m pytest test/kohakuhub -p scripts.ci.select_tests --ci-category fast -q
```

To inspect selection without executing tests, replace `fast` with any category:

```sh
KOHAKU_HUB_DB_BACKEND=sqlite KOHAKU_HUB_DATABASE_URL=sqlite:///:memory: PYTHONPATH=src:. python -m pytest test/kohakuhub -p scripts.ci.select_tests --ci-category fast --collect-only -q
```

Service jobs use `scripts/ci/compose.yml`, not the development or production stack.
The workflow sets test-only database/S3 credentials and endpoints, starts the needed
containers with `docker compose up --wait`, and removes only that CI project's
containers and volumes in an `always()` step. Existing test bootstrap creates the
S3 bucket and LakeFS credentials in ignored `hub-meta/test/backend-service/`.
The seeded suite resets its test database schema and storage; it must not point at
production or reused development data. The Compose file accepts `CI_POSTGRES_PORT`,
`CI_MINIO_PORT`, `CI_LAKEFS_PORT` and `CI_VALKEY_PORT` for isolated local experiments;
the corresponding test endpoint environment variables must match those ports.

Only XML reports and logs under ignored `lint-reports/ci/` are retained as GitHub
artifacts for seven days. Credential files, databases and `.env` files are excluded.
Checkout has no persisted credentials; workflows request only `contents: read`.

Actions are pinned to official full commit SHAs (verified against their official v6
tags on 2026-10-04). Update pins explicitly when reviewing new releases:

- [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- [actions/checkout](https://github.com/actions/checkout)
- [actions/setup-python](https://github.com/actions/setup-python)
- [actions/upload-artifact](https://github.com/actions/upload-artifact)

Local YAML/actionlint, Compose rendering, selector collection and selected regressions
can validate this configuration. They do not constitute a completed GitHub Actions
run or a full service/HF matrix result.
