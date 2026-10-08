# 前瞻性 huggingface_hub CI

[English](hf-forward-ci.md) | 简体中文

`.github/workflows/hf-forward.yml` 每天 UTC 03:23 使用**最新版** `huggingface_hub` 运行，
与手动回归 workflow 相互独立，只运行标记为 `hf_client` 的测试，即直接调用 `huggingface_hub` 的用例：

```sh
python -m pytest test/kohakuhub -m hf_client --collect-only -q   # 列出这些用例
```

任务使用与回归相同的隔离服务栈。失败时 `scripts/ci/hf_forward_triage.py` 决定如何上报：

1. **完全重复。** 失败签名（失败用例 ID 与首行信息）与某个带 `hf-forward-ci` 标签的开放 issue
   正文中的隐藏标记一致，脚本只在该 issue 下追加评论（含运行链接与版本），不新开 issue。
2. **同一问题、措辞不同。** 否则模型 `claude-haiku-5-5` 接收失败用例、日志末尾和开放 issue 列表，
   返回重复 issue 编号（必须是列表中的开放 issue）或新的标题与正文。重复则评论；新问题只新建一个 issue。
3. **无法访问模型**（例如未配置 secret 的 fork），只执行第 1 条规则并使用通用 issue 文本。
   模型出错会打印原因并同样回退。

## Secrets

模型通过仓库 secret `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN` 配置的 Anthropic 兼容网关调用。
它们的值不会出现在日志或 issue 中。轮换位置：**Settings → Secrets and variables → Actions**。

## 复用报告（人工与 Claude Code）

每次运行上传 `hf-forward-<run id>-<attempt>`，保留 14 天：

| 文件 | 内容 |
| --- | --- |
| `hf-forward.xml` | `hf_client` 用例的 JUnit 报告 |
| `hf-forward.log` | 测试输出 |
| `hf-forward-triage.json` | 决定：`create` 或 `comment`、issue 编号与签名 |
| `services.log` | 服务容器日志 |

```sh
gh run list -R cheesecave/cheesecave-backend --workflow hf-forward.yml -L 5
gh run download <run-id> -R cheesecave/cheesecave-backend -n hf-forward-<run-id>-1 -D /tmp/hf-forward
```

手动运行时将 `dry_run` 设为 `true`，即可只看决定而不写入 issue。

## 限制

绿色运行只说明最新版下 `hf_client` 用例通过，不代表其他客户端（transformers、datasets、`hf` CLI、
git/LFS）也兼容。模型负责判断重复，可能判断错误，行动前请核对链接的 issue。
