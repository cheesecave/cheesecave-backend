# 客户端兼容性矩阵

[English](compatibility.md) | 简体中文

说明实际测试了什么、在哪里测试，以及不声明什么。版本来自 CI workflow。

| 组件 | 覆盖版本 | 运行位置 |
| --- | --- | --- |
| Python | 3.10、3.11、3.12（单元测试）；3.12（服务与 HF） | `backend-tests`、`backend-regression` |
| huggingface_hub | 0.20.3、1.0.1、latest（整套）；0.36.2、2.0.0（仅客户端用例）每个 PR；0.30.2、1.6.0 每日 | `backend-tests`，[前瞻性 CI](hf-forward-ci.zh-CN.md) |
| PostgreSQL | 15 | 服务栈 |
| LakeFS | 1.86.0；1.48.1（关闭缓存，每个 PR）、1.86.0（关闭缓存，每日） | 服务栈、`backend-tests` |
| MinIO（S3 API） | compose 中固定的 `pgsty/minio` 版本 | 服务栈 |
| Valkey（缓存） | 8 | 服务栈 |

## 不声明

- Xet 存储。`huggingface_hub` 2.0 及以后的版本默认让 LFS 大小的文件走 Xet，除非设置 `HF_HUB_DISABLE_XET=1`（测试套件设置了它）。
  不计划支持，见 issue #71。
- transformers、diffusers、datasets、`hf` CLI，以及使用 LFS 的 git-over-HTTP 客户端。
- 除路径式测试之外，bucket 名称出现在 S3 endpoint 中的部署方式。
- 早于上表所列的 LakeFS 版本，以及 CI runner 之外的 Docker 主机。

只有 workflow 变化时矩阵才会变化。升级固定版本或新增客户端，需要在同一个 PR 中修改矩阵。
