---
title: 文件分支收缩（迁移 033）
description: 每分支 File 记录（issue #11）收缩步骤的运维手册
icon: i-carbon-branch
---

# 文件分支收缩（迁移 033）

本文是每分支 `File` 记录（issue #11）收缩（CONTRACT）步骤的运维手册。扩展（EXPAND，迁移 032）在启动时运行，生产环境已经应用。收缩（033）由运维人员手动运行一次。它不在启动时运行，也不放在迁移运行器的目录中。英文原文见 [file-branch-contract.md](file-branch-contract.md)。

下文每条 `docker run` 命令都使用本变更或其之后构建的后端镜像（该镜像包含 `scripts/db_migrations/contract/preflight_033.py` 和 `032 --rollback`）。后端副本可以继续运行它们已在运行的镜像（`d3140a2` 或之后的版本），收缩不依赖副本镜像。

下文使用的占位符：

- `$IMAGE`：本变更或之后构建的后端镜像。
- `<env-file>`：后端副本使用的环境变量文件（数据库地址和存储设置）。
- `postgres`：示例 Compose 文件中的 PostgreSQL 容器名。请换成你的容器名。
- `kohakuhub`：数据库名。

## 1. 用途，以及收缩改变了什么

每分支的 `File` 记录让仓库的每个分支都保存自己的文件记录。迁移 032（扩展）在不破坏正在运行的代码的前提下实现了这一点：

- `file.branch` 列存在，默认值为 `main`。已有行变为 `main`。
- 唯一索引 `file_repository_id_branch_path_in_repo` 在 `(repository_id, branch, path_in_repo)` 上存在且有效。
- 分支化之前的唯一索引 `file_repository_id_path_in_repo`（在 `(repository_id, path_in_repo)` 上）保留下来。旧代码的 `ON CONFLICT (repository_id, path_in_repo)` 依赖它。只要它存在，对 `main` 已有路径的分支提交会被拒绝，返回 HTTP 409（`BranchPathConflict`）。

收缩（033）在一个事务中做三件事：

1. 删除旧唯一索引 `file_repository_id_path_in_repo`。此后允许对 `main` 已有路径的分支写入。
2. 创建一个同名的空的非唯一索引：`CREATE INDEX file_repository_id_path_in_repo ON file (repository_id, path_in_repo) WHERE FALSE`。名称仍被占用。这一点很重要：旧代码会声明它的唯一索引，并在每次启动时用 `CREATE UNIQUE INDEX IF NOT EXISTS` 重建它。名称被占用后，这条语句什么也不做，拒绝规则不会恢复，旧代码的 `ON CONFLICT (repository_id, path_in_repo)` 找不到唯一索引，因而失败。
3. 不改动任何行。没有行被重写；收缩只是目录变更加一个空索引。

除此之外模式不变。当前代码（`d3140a2` 及之后）在收缩前后都能照常运行。

## 2. 何时可以安全执行

只有当以下条件全部成立时才运行收缩。第 1 步和第 3 步会检查它们。

1. 每个后端副本都运行 `d3140a2` 或之后的版本，即写入 `branch` 的代码。没有其他副本提供写服务。
2. 任何早于 `d3140a2` 的镜像都不会再运行。旧镜像应当退役，而不是留作以后重启。这无法从数据库中检查，是运维人员的决定。
3. 扩展 032 已应用且有效：存在 `branch` 列、有效的 `file_repository_id_branch_path_in_repo`，以及 `schema_rollout` 表。预检会检查这三项。
4. 已完成数据库备份并验证（第 2 步）。
5. `file` 表上没有长时间未结束的事务。收缩会短暂持有 `file` 上的排他锁。

生产环境当前运行 `d3140a2`，扩展 032 已应用，收缩尚未运行（以携带本手册的发布为准）。

## 3. 不可逆点，以及回滚的含义

收缩之前，没有任何操作不可逆：

- 删除标记行（`DELETE FROM schema_rollout WHERE name = 'file_branch_contract';`）即可重新关闭闸门。扩展本身可以保留：当前代码与它一起工作，对共享路径的分支提交仍像现在一样被拒绝。

收缩成功即是不可逆点。之后：

- 旧唯一索引已经消失。旧代码对 `main` 的写入会失败（它的 `ON CONFLICT` 没有可匹配的唯一索引）。它的启动不会被拒绝，但它的写入会失败，数据不会落库。这就是执行强制，也是为什么不能再运行任何旧镜像。
- 原地恢复旧唯一索引（`032 --rollback`，见第 6 节）要求**没有非 main 的文件行**。只要有任何行的 `branch <> 'main'`，命令就会拒绝执行，因为分支化之前的模式容纳不了这样的行。删除这些行是运维人员的数据决定，命令不会替你做。
- 另一条回退路径是恢复第 2 步的备份。这会把数据库恢复到备份时的状态，备份之后的所有写入都会丢失。这是最后手段，在副本停止时进行。

## 4. 步骤

按顺序执行。遇到第一个失败就停下，并按第 5 节的中止规则处理。

### 第 1 步：验证副本

每个副本报告的 `build.git_sha` 都必须包含 `d3140a2`。版本接口是公开的，构建标识位于 `build` 之下：

```sh
curl -fsS http://<replica>:48888/api/version
```

用代码仓库（本仓库的检出，而不是副本）核对报告的 `build.git_sha`：

```sh
git -C <checkout> merge-base --is-ancestor d3140a2 <build.git_sha> && echo "ok: <replica> runs d3140a2 or later"
```

预期：每个副本都输出 `ok`。如果 `build.git_sha` 为 `null`，说明镜像构建时没有设置 `KOHAKU_HUB_GIT_SHA`。在确认镜像是 `d3140a2` 或之后的版本（例如通过构建记录）之前不要继续。任何运行旧镜像的副本：停止它，让其中的进行中请求结束，并保持停止。不要重新启动它。

### 第 2 步：备份数据库

```sh
mkdir -p backups
docker exec postgres pg_dump -U hub -Fc kohakuhub > backups/file-branch-pre-033-$(date -u +%Y%m%dT%H%M%SZ).dump
sha256sum backups/file-branch-pre-033-*.dump
docker exec -i postgres pg_restore --list < backups/file-branch-pre-033-<timestamp>.dump > /dev/null && echo "backup readable"
```

预期：得到一个非空的转储文件、你需要记录的校验和，以及 `backup readable`。无法列出内容的转储不是备份：停止。

### 第 3 步：只读检查

运行预检。它只读取数据：所有检查都在一个只读事务中完成，且从不提交。

```sh
docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/contract/preflight_033.py
echo "exit=$?"
```

预期：`exit=0`；`expand_032`、`new_key_valid`、`old_key`、`rollout_table` 和 `shared_path_rows` 都有 `PASS` 行；并输出 `preflight: all checks passed`。备注会给出 `main` 上和非 main 分支上的文件行数，以及标记状态（`not set`）。

另外检查是否有进程在数据库上持有未结束的事务：

```sh
docker exec postgres psql -U hub -d kohakuhub -tAc \
  "SELECT pid, now() - xact_start, state FROM pg_stat_activity WHERE datname = current_database() AND xact_start < now() - interval '30 seconds'"
```

预期：没有输出行。长事务会阻塞收缩的锁；等它结束，或者中止。

### 第 4 步：插入标记行

标记表示运维人员确认第 1 步成立：每个副本都运行 `d3140a2` 或之后的版本，并且旧镜像已经退役。只有到那时才插入：

```sh
docker exec postgres psql -U hub -d kohakuhub -v ON_ERROR_STOP=1 -c \
  "INSERT INTO schema_rollout (name, set_at) VALUES ('file_branch_contract', CURRENT_TIMESTAMP);"
```

预期：`INSERT 0 1`。用 `SELECT name, set_at FROM schema_rollout;` 核对。

### 第 5 步：运行收缩

```sh
docker run --rm --env-file <env-file> -e PGOPTIONS='-c lock_timeout=10s' $IMAGE \
  python scripts/db_migrations/contract/033_file_branch_contract.py
echo "exit=$?"
```

预期：`Migration 33: old key dropped, non-main File rows enabled`，并且 `exit=0`。

`lock_timeout` 让收缩在等待超过 10 秒时放弃，而不是排在长事务后面，同时让 `file` 上的其他查询全部等待。如果它放弃了，什么都没有改变（变更是一个事务）；见第 5 节。

再次运行收缩是无害的：它会发现防护索引已在位，并执行同样的操作。

### 第 6 步：验证

检查索引状态。新键是唯一且有效的；旧名称是非唯一的空防护索引：

```sh
docker exec postgres psql -U hub -d kohakuhub -tAc \
  "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'file' AND indexname IN ('file_repository_id_branch_path_in_repo', 'file_repository_id_path_in_repo')"
```

预期：分支键是 `CREATE UNIQUE INDEX ... (repository_id, branch, path_in_repo)`；旧名称是 `CREATE INDEX ... (repository_id, path_in_repo) WHERE false`（没有 `UNIQUE`）。

再次运行预检。此时它必须在 `old_key` 上失败，并提示收缩已经运行（该键已是非唯一的防护索引）。其他检查都必须通过。这个失败正是收缩已完成的特征：

```sh
docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/contract/preflight_033.py
# 预期：exit=1，FAIL old_key（"is not unique: the contract already ran ..."），其余都是 PASS
```

然后通过公开 API，在你拥有的仓库上验证收缩所要实现的行为：

1. 对一个分支的提交，若它改动了 `main` 已有的路径，现在返回 `200`（收缩前返回 `409 BranchPathConflict`）。
2. 读取 `main` 仍然返回 `main` 的对象 ID（对 `main` 执行 `tree`、`paths-info` 和 `resolve`）。

### 第 7 步：监控

收缩后至少监控 30 分钟。出现以下任何情况都说明有问题，见第 5 节：

```sh
# 旧镜像的写入：旧的 ON CONFLICT 不再匹配任何东西
docker logs --since 30m <replica> 2>&1 | grep -c "no unique or exclusion constraint matching the ON CONFLICT"
```

另外，在反向代理或访问日志中统计错误码为 `BranchPathConflict` 的 `409` 响应。收缩后这个数字必须停止增长：拒绝已经不会发生了。

预期：在每个运行 `d3140a2` 或之后版本的副本上，上述日志搜索的结果都是 `0`。

## 5. 中止规则

| 情况 | 操作 | 原因 |
|---|---|---|
| 第 1 步发现某个副本早于 `d3140a2`，或无法确认其 `build.git_sha` | 停止该副本并保持停止。不要插入标记。 | 旧代码通过收缩将删除的键写入 `main`。 |
| 第 2 步：转储失败或无法读取 | 停止，不继续。 | 没有备份就不做收缩。 |
| 第 3 步退出码为 `1` | 读 `FAIL` 行。`expand_032` 或 `rollout_table` 失败，说明 032 没有完成：运行 `docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/032_file_branch_expand.py`（它会完成扩展），然后重做第 3 步。`new_key_valid` 显示 `INVALID`，说明构建被中断：同一命令会重建它。`shared_path_rows` 失败说明状态已损坏：停止并先调查。 | 预检是有意拒绝的；收缩本身也会拒绝，甚至更糟。 |
| 第 3 步：有长事务未结束 | 等它结束，或停止持有它的进程。重做第 3 步。 | 收缩需要对 `file` 的短暂锁。 |
| 第 4 步失败 | 没有任何改变。修复原因后重新插入。 | |
| 第 5 步输出 `marker row is missing` 并退出码为 `1` | 标记没有插入（或名称不对）。插入标记（第 4 步），然后重新运行第 5 步。 | 闸门关闭；没有任何改变。 |
| 第 5 步输出 `expand (032) is not applied` | 运行 032，然后从第 3 步重做。 | 没有扩展，收缩拒绝执行，因此旧键绝不会成为唯一的防护。 |
| 第 5 步等锁超时（`canceling statement due to lock timeout`）并退出码为 `1` | 没有任何改变。找出长事务（第 3 步），等待，然后重新运行第 5 步。 | `lock_timeout` 阻止排队等待。 |
| 第 5 步因其他原因失败 | 没有任何改变：变更是一个事务。读取报错信息，修复原因，重新运行第 5 步。不要手动修改数据库。 | |
| 第 6 步：索引状态不符合预期，或分支提交没有返回 `200` | 停止。与备份和第 6 节对比。不要手动添加或删除索引。 | 索引状态异常说明收缩没有按原样执行。 |
| 第 7 步：出现 `ON CONFLICT` 错误 | 立即停止输出这些错误的副本。它的写入会失败，因此没有数据经它落库，但它的客户端会收到错误。确认它是哪个镜像：它是旧镜像，必须保持停止。 | 旧镜像绝不能再运行。 |

## 6. 回滚

收缩之后，回滚有两种形式。两者都由运维人员在**后端副本停止**时执行。

**A. 原地：恢复分支化之前的模式。** 这是命令。它删除 `file.branch`，恢复旧唯一索引，删除标记表和新键；只要存在任何非 main 行，它就拒绝执行：

```sh
docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/032_file_branch_expand.py --rollback
echo "exit=$?"
```

预期：当所有行都在 `main` 上时，`exit=0` 并输出 `rolled back to the pre-A schema`。只要有任何行的 `branch <> 'main'`，就会 `exit=1` 并输出 `refusing to roll back issue #11: N file row(s) on a non-main branch ...`。这种情况下不会有任何改变。原地恢复旧键不需要非 main 行；该命令会强制执行这一点。

**B. 从备份恢复。** 当存在非 main 行，而你不想删除它们时使用。恢复第 2 步的转储。这会把数据库恢复到备份时的状态：已扩展、旧键存在、标记不存在。备份之后的所有写入都会丢失。

```sh
docker exec -i postgres pg_restore -U hub --clean --if-exists -d kohakuhub < backups/file-branch-pre-033-<timestamp>.dump
```

恢复后运行预检。它必须通过，标记必须不存在，并且旧键必须重新唯一。然后启动运行 `d3140a2` 或之后版本的副本。

为了让 A 形式可以执行而删除非 main 行，是数据决定（这些分支的文件记录会丢失）。这不属于本手册的范围。

收缩之前不需要回滚：第 3 节说明了这种情况。

## 7. 运维人员绝不能做的事

- **绝不启动旧镜像。** 指任何早于 `d3140a2`（分支化之前的代码）构建的镜像。它的启动不会被拒绝，但收缩后它的写入会失败。它仍是旧代码的固定副本，绝不能对用户提供服务。
- **绝不在没有标记行的情况下运行收缩。** 脚本没有标记会拒绝执行，并且不改变任何东西。不要为了越过自己不理解的拒绝而插入标记，也不要用 SQL 绕过脚本。
- **绝不从自动运行目录运行收缩。** 不要把 `033_file_branch_contract.py` 复制到 `scripts/db_migrations/`。运行器会在每次容器启动时运行顶层的每个迁移，任一失败就以非零退出：关闭的闸门会让每个容器都无法启动。
- **绝不手动重建旧键，也不删除防护索引。** 两者任一都会让旧启动恢复拒绝规则，或者让旧写入者重新拿到旧键。
- **副本仍在服务时绝不运行 `032 --rollback`。** 它的检查与写入竞争。
- **除了第 4 步插入标记，以及中止路径上删除标记之外，绝不修改 `schema_rollout`。**
- **绝不在没有备份的数据库上运行预检或收缩。** 预检不写入任何东西，但收缩是不能原地撤销的步骤。
