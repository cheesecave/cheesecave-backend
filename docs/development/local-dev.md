# Standalone backend development

Work from cheesecave-backend. Python 3.10+, Docker and Bash (WSL or Git Bash on
Windows) are needed for the existing service helpers. Frontend Node/pnpm
dependencies belong to the sibling web/admin repositories.

```sh
python -m venv venv
# Activate the environment for your shell.
pip install -e ".[dev]"
make init-env
make infra-up
make backend
# In another terminal, after the API has initialized credentials and tables:
make worker
```

The persisted PostgreSQL, MinIO, LakeFS and Valkey services, legacy KOHAKU_HUB
variables and `hub-meta/dev` paths are unchanged. Edit ignored `.env.dev` and
reuse its persisted LakeFS credentials when restarting. Start each UI from its
own checkout using its development instructions. The API remains on :48888;
web uses :5173 and admin :5174 in the standard sibling setup.

```sh
make seed-demo
make verify-seed-demo
make test-backend
make test-backend RANGE_DIR=api/repo/routers
```

Backend tests mirror `src/kohakuhub` under `test/kohakuhub`. `make test` runs
only the backend; frontend suites are run from their respective repositories.
Full backend coverage retains its 80% gate. Dependencies and fixtures from
PostgreSQL, MinIO and LakeFS are required for service-backed tests. Inherited
GitHub Actions and Codecov configuration were removed before splitting; run
Python/huggingface_hub compatibility and storage-layout checks locally as needed.

`make infra-down` stops development services while preserving data.
`make reset-local-data` and `make reset-and-seed` erase persisted local demo
data through the existing warning/confirmation helper. These are development
operations; they are not a production migration or backup workflow.

For complete sibling source images or independent release deployment, see
[Docker deployment](../deployment/docker.md). Do not apply old monorepo root
pnpm targets or dist-directory mounts to this backend checkout.
