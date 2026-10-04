# CheeseCave backend scripts

Run commands from this backend checkout. No script installs or builds frontend
dependencies. Web/admin maintain their own package scripts and CI.

| Script | Purpose |
| --- | --- |
| generate_docker_compose.py --generate-config | Create an ignored .env with secrets and image references; refuse overwrite |
| generate_docker_compose.py --output PATH | Copy the maintained standalone compose.yml |
| deploy.py | Print native Compose instructions; does not deploy |
| format.py | Run Black with line length 100 on backend Python paths |
| run_migrations.py | Run the preserved numbered database migrations |
| generate_secret.py | Generate credentials for manual configuration |
| migrate_config.py | Preserve/convert legacy backend config formats |
| clear_s3_storage.py / show_s3_usage.py | Storage administration; review targets before destructive use |
| dev/run_backend.sh / dev/run_worker.sh | Start API / background worker against local persisted services |
| dev/up_infra.sh / dev/down_infra.sh | Manage local development dependencies |
| dev/seed_demo_data.py / dev/verify_seed_data.py | Deterministic local demo fixtures |

Native Compose supports release images and sibling source builds. See
[deployment instructions](../docs/deployment/docker.md). The former
`kohakuhub.conf` interactive generator format is retired; retain existing
production settings in `.env` and ordinary Compose override files. Existing
KohakuBoard integration helpers are inherited optional examples, not part of
the three CheeseCave services or their release deployment.
