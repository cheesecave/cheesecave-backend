# 客户端兼容性矩阵

[English](compatibility.md) | 简体中文

说明实际测试了什么、在哪里测试，以及不声明什么。版本来自 CI workflow。

| 组件 | 覆盖版本 | 运行位置 |
| --- | --- | --- |
| Python | 3.10、3.11、3.12（单元测试）；3.12（服务与 HF） | `backend-tests`、`backend-regression` |
| huggingface_hub | 0.20.3、0.36.2、1.6.0（固定）；latest（每日） | `backend-regression` 的 `hf`，[前瞻性 CI](hf-forward-ci.zh-CN.md) |
| PostgreSQL | 15 | 服务栈 |
| LakeFS | 1.86.0；1.48.1（关闭缓存） | 服务栈、`backend-tests` |
| MinIO（S3 API） | compose 中固定的 `pgsty/minio` 版本 | 服务栈 |
| Valkey（缓存） | 8 | 服务栈 |

## 不声明

- transformers、diffusers、datasets、`hf` CLI，以及使用 LFS 的 git-over-HTTP 客户端。
- 除路径式测试之外，bucket 名称出现在 S3 endpoint 中的部署方式。
- 早于上表所列的 LakeFS 版本，以及 CI runner 之外的 Docker 主机。

只有 workflow 变化时矩阵才会变化。升级固定版本或新增客户端，需要在同一个 PR 中修改矩阵。
