# Client compatibility matrix

English | [简体中文](compatibility.zh-CN.md)

What is exercised, where, and what is not claimed. Versions come from the CI workflows.

| Component | Versions covered | Where it runs |
| --- | --- | --- |
| Python | 3.10, 3.11, 3.12 (unit tests); 3.12 (services, HF) | `backend-tests`, `backend-regression` |
| huggingface_hub | 0.20.3, 0.36.2, 1.6.0 (pinned); latest (daily) | `backend-regression` `hf`, [forward CI](hf-forward-ci.md) |
| PostgreSQL | 15 | services stack |
| LakeFS | 1.86.0; 1.48.1 with cache disabled | services stack, `backend-tests` |
| MinIO (S3 API) | `pgsty/minio` release pinned in compose | services stack |
| Valkey (cache) | 8 | services stack |

## Not claimed

- transformers, diffusers, datasets, the `hf` CLI, and git-over-HTTP clients with LFS.
- Deployments where the bucket name appears inside the S3 endpoint, beyond the path-style test.
- Older LakeFS releases than those listed, and any Docker host other than the CI runner.

A row changes only when a workflow changes. Upgrading a pin or adding a client means editing the
matrix in the same pull request.
