# 后端手动回归 CI

[English](ci.md) | 简体中文

仓库 Actions 已启用。本回归 workflow 仍只能手动运行：唯一事件为 `workflow_dispatch`，
没有 push、PR、定时、部署、镜像发布或向第三方上传覆盖率的任务。每日前瞻性 huggingface_hub
检查是独立 workflow，见 [前瞻性 HF CI](hf-forward-ci.zh-CN.md)。

| 分类 | 范围 | 基础设施 |
| --- | --- | --- |
| `fast` | Python 3.10/3.12 的现有单测与选定的隔离 SQLite/API 回归 | 无 |
| `migrations` | SQLite/PostgreSQL 模式、保留数据的升级、重试和迁移顺序 | PostgreSQL 15 |
| `services` | 完整后端套件、原有 80% 覆盖率门槛、缓存开启/关闭 | PostgreSQL、MinIO、LakeFS 1.86.0、Valkey |
| `hf` | 现有真实 HF 客户端及 fallback 互操作测试；Python 3.12；客户端 0.20.3、0.36.2、1.6.0、latest | 完整隔离栈 |
| `all` | 上述全部任务 | 各任务独立 runner 和临时数据 |

分类器只在明确传入参数时生效，普通 pytest 行为不变。`fast` 排除 integration 参数及
使用完整服务夹具的测试；`migrations` 排除需要完整已初始化服务的旧迁移测试，后者由
`services` 覆盖。迁移分类还会在导入前排除无关测试模块，避免路由模块初始化完整应用
模式而干扰旧模式迁移测试。分类为空时失败。测试可能因客户端不支持某接口、SQLite 限制或部署
配置而跳过，原因保留在 JUnit 和日志中；不能据此宣称覆盖所有 Python/HF/LakeFS 版本
及 bucket-in-endpoint 部署组合。

workflow 已集成到默认分支 `main`。在 GitHub Actions UI 选择
**Backend regression (manual only)**、分支和分类即可手动运行；编辑本说明不会触发运行。

## 本地命令与隔离

安装 `.[dev]` 后，可运行无需服务的分类：

```sh
KOHAKU_HUB_DB_BACKEND=sqlite KOHAKU_HUB_DATABASE_URL=sqlite:///:memory: PYTHONPATH=src:. python -m pytest test/kohakuhub -p scripts.ci.select_tests --ci-category fast -q
```

仅检查选择结果时，分类可替换为任意分类：

```sh
KOHAKU_HUB_DB_BACKEND=sqlite KOHAKU_HUB_DATABASE_URL=sqlite:///:memory: PYTHONPATH=src:. python -m pytest test/kohakuhub -p scripts.ci.select_tests --ci-category fast --collect-only -q
```

服务任务使用 `scripts/ci/compose.yml`，使用测试专用地址与凭证，并通过 `up --wait`
等待就绪。现有测试 bootstrap 创建 S3 bucket 和被忽略的 LakeFS 测试凭证。
`always()` 清理步骤只移除本次 CI 项目的容器与卷。已初始化套件会重置测试数据库
schema 与存储，必须使用临时测试数据，不能连接生产或复用开发数据。
Compose 支持 `CI_POSTGRES_PORT`、`CI_MINIO_PORT`、`CI_LAKEFS_PORT`、`CI_VALKEY_PORT`；
本地试验改变端口时，需要同步对应测试地址环境变量。

仅将被忽略的 `lint-reports/ci/` 中的 XML 和日志作为 GitHub artifact 保留七天，不上传
凭证、数据库和 `.env`。checkout 不持久保存凭证，权限仅为 `contents: read`。

三个官方 action 固定为完整 SHA，2026-10-04 已核对官方 v6 tag：
[workflow 语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、
[checkout](https://github.com/actions/checkout)、
[setup-python](https://github.com/actions/setup-python)、
[upload-artifact](https://github.com/actions/upload-artifact)。升级时需明确复核新的 SHA。

本地 YAML/actionlint、Compose 渲染、分类收集和选定回归可以验证配置，但不能代替实际
GitHub Actions 运行，也不表示完整服务/HF 矩阵已经通过。
