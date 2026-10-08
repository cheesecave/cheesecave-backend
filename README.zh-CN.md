# CheeseCave Backend

[English](README.md) | 简体中文

CheeseCave 是一个可自行部署、兼容 Hugging Face 客户端的模型与数据集仓库服务，
由 KohakuHub 分叉并拆分为三个独立开发的仓库。

| 仓库 | 职责 |
| --- | --- |
| [cheesecave-backend](https://github.com/cheesecave/cheesecave-backend) | API、Git/LFS、后台任务、存储、数据库迁移及统一 Compose 配置 |
| [cheesecave-web](https://github.com/cheesecave/cheesecave-web) | 主站、仓库浏览、文件与数据集查看 |
| [cheesecave-admin](https://github.com/cheesecave/cheesecave-admin) | 管理后台、用户与配额管理、运行配置 |

本仓库包含 FastAPI 后端、worker 和基础设施配置。两个前端各自构建静态镜像，
可以独立升级；后端不依赖前端源码或本地 `dist` 目录。

## 本地开发

需要 Python 3.10+、Docker，以及 Bash；Windows 可使用 WSL 或 Git Bash 运行 Make 命令。

```sh
git clone https://github.com/cheesecave/cheesecave-backend.git
cd cheesecave-backend
python -m venv venv
# 按当前 shell 激活虚拟环境后安装依赖。
pip install -e ".[dev]"
make init-env
# 根据实际环境编辑忽略的 .env.dev。
make infra-up
make backend
```

API 默认监听 `http://localhost:48888`。API 完成表和凭据初始化后，在另一终端运行
`make worker`。前端开发服务从各自仓库启动。

```sh
make test
# 只检查指定的后端模块：
make test-backend RANGE_DIR=api/repo/routers
```

完整后端测试需要 PostgreSQL、MinIO 和 LakeFS 等服务；完整覆盖率检查保留 80% 门槛。
参阅 [本地开发说明](docs/development/local-dev.md)。继承的 GitHub Actions 和 Codecov
配置已在拆分前移除，本地测试和构建命令仍可使用。

## 快速开始：直接运行已发布的镜像

无需构建，也不需要其他两个仓库；Compose 会拉取
`ghcr.io/cheesecave/cheesecave-{backend,web,admin}`。

```sh
git clone https://github.com/cheesecave/cheesecave-backend.git
cd cheesecave-backend
python scripts/generate_docker_compose.py --generate-config   # 生成带随机凭据的 .env
docker compose up -d
```

访问 `http://127.0.0.1:28080`（管理后台在 `/admin/`）。在 `.env` 设置
`CHEESECAVE_VERSION` 可固定版本，默认为 `latest`。

## 从源码部署整个项目

将三个仓库放在同一父目录中：

```text
CheeseCave/
  cheesecave-backend/
  cheesecave-web/
  cheesecave-admin/
```

在后端目录执行：

```sh
python scripts/generate_docker_compose.py --generate-config
# 编辑生成的 .env：外部地址、凭据及 UID/GID。
docker compose -f compose.yml -f compose.build.yml config
docker compose -f compose.yml -f compose.build.yml up -d --build
```

配置生成器只准备配置和随机凭据，不启动服务；持久化目录的权限及已有数据迁移准备见
[部署说明](docs/deployment/docker.md)。默认网关端口为 `28080`，主站位于 `/`，
管理后台位于 `/admin/`。API 与 worker 必须使用同一后端镜像。

如需使用其他镜像，可在 `.env` 分别设置 `CHEESECAVE_BACKEND_IMAGE`、
`CHEESECAVE_WEB_IMAGE` 和 `CHEESECAVE_ADMIN_IMAGE`；未设置时，源码构建叠加文件会标记为 `cheesecave-*:local`。

## 独立更新与兼容性

仅更新主站时，修改 `.env` 中的主站镜像引用，然后执行：

```sh
docker compose pull hub-web
docker compose up -d --no-deps hub-web
```

更新管理后台则替换为 `hub-admin`。使用兄弟仓库源码更新时可运行
`docker compose -f compose.yml -f compose.build.yml up -d --build --no-deps hub-web`，
Admin 同理。网关会重新解析替换后容器的地址。更新后端时应同时更新 `hub-api` 和
`khub-worker`，并核对数据库迁移及后台任务兼容性；回滚步骤见部署文档。

项目与发行名称改为 CheeseCave，Python 导入仍为 `kohakuhub`。`KOHAKU_HUB_*` 配置、
协议身份、数据库迁移、存储标识和既有客户端路径继续保留。默认展示名为 CheeseCave，
管理员已配置的品牌优先。新前端能力需要相应后端 API 支持；破坏兼容的变更应明确记录。

## 来源与许可证

本项目源自 [deepghs/KohakuHub](https://github.com/deepghs/KohakuHub)，
上游为 [KohakuBlueleaf/KohakuHub](https://github.com/KohakuBlueleaf/KohakuHub)。
保留 KohakuBlueLeaf、DeepGHS 及原贡献者的署名和版权信息，属于独立分叉项目。

后端保留全部原远端提交历史，CI 移除、拆分和后续改动以新提交追加；两个前端使用全新历史，
首个提交为「从原仓库分叉」。来源与历史策略见 [provenance/UPSTREAM.md](provenance/UPSTREAM.md)，
原项目资料见 [原 README](provenance/README.upstream.md) 和
[原 CHANGELOG](provenance/CHANGELOG.upstream.md)。

[LICENSE](LICENSE) 和 [LICENSING.md](LICENSING.md) 保留原文。Dataset Viewer 是唯一带独立许可证的组件，
已于 2026-10-08 移除，本仓库的代码均适用 AGPL-3.0，详见 [NOTICE.md](NOTICE.md)。
原许可说明中的 monorepo 路径属于历史资料，拆分后的当前路径见上述链接。

修改及署名声明见 [NOTICE.md](NOTICE.md)。

后端已准备 [分类手动回归 CI](docs/development/ci.zh-CN.md)，供以后运行；添加这些配置不会启用或触发 GitHub Actions。
