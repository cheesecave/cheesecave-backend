---
title: File branch contract (migration 033)
description: Operator runbook for the contract step of per-branch File rows (issue #11)
icon: i-carbon-branch
---

# File branch contract (migration 033)

This is the runbook for the CONTRACT step of per-branch `File` rows (issue #11). The EXPAND (migration 032) runs at startup and is already applied in production. The CONTRACT (033) is run by an operator, by hand, once. It is not run at startup and is not in the migration runner's directory. The Chinese companion is [file-branch-contract.zh-CN.md](file-branch-contract.zh-CN.md).

Every `docker run` below uses a backend image built from this change or later (the image that contains `scripts/db_migrations/contract/preflight_033.py` and `032 --rollback`). Replicas may keep running the image they already run (`d3140a2` or later); the contract does not depend on the replica image.

Placeholders used below:

- `$IMAGE`: the backend image built from this change or later.
- `<env-file>`: the environment file the backend replicas use (the database URL and the storage settings).
- `postgres`: the PostgreSQL container name in the sample Compose file. Use yours.
- `kohakuhub`: the database name.

## 1. Purpose and what the contract changes

Per-branch `File` rows let every branch of a repository keep its own file records. Migration 032 (expand) made that possible without breaking the running code:

- `file.branch` exists, with default `main`. Existing rows become `main`.
- A unique key `file_repository_id_branch_path_in_repo` on `(repository_id, branch, path_in_repo)` exists and is valid.
- The pre-branch unique key `file_repository_id_path_in_repo` on `(repository_id, path_in_repo)` is kept. It is what the older code's `ON CONFLICT (repository_id, path_in_repo)` relies on. While it exists, a side-branch commit that changes a path `main` already has is refused with HTTP 409 (`BranchPathConflict`).

The contract (033) does three things, in one transaction:

1. Drops the old unique key `file_repository_id_path_in_repo`. Side-branch writes to paths `main` already has are now allowed.
2. Creates an empty, non-unique index with the same name, `CREATE INDEX file_repository_id_path_in_repo ON file (repository_id, path_in_repo) WHERE FALSE`. The name stays taken. This matters: the older code declares its unique key and re-creates it with `CREATE UNIQUE INDEX IF NOT EXISTS` at every boot. With the name taken, that statement does nothing, so the refusal is not restored, and an older writer's `ON CONFLICT (repository_id, path_in_repo)` finds no unique key and fails.
3. Changes no rows. Nothing is rewritten; the contract is a catalog change plus an empty index.

Nothing else changes in the schema. The current code (`d3140a2` and later) runs unchanged before and after.

## 2. When it is safe

Run the contract only when all of these hold. Step 1 and step 3 of the procedure check them.

1. Every backend replica runs `d3140a2` or later, the code that writes `branch`. Nothing else serves writes.
2. No image older than `d3140a2` will ever run again. Old images are retired, not parked for a later restart. This cannot be checked from the database; it is an operator decision.
3. Expand 032 is applied and valid: the `branch` column, a valid `file_repository_id_branch_path_in_repo`, and the `schema_rollout` table. The preflight checks all three.
4. A backup of the database was taken and verified (step 2).
5. No transaction has been open on `file` for long. The contract takes a short exclusive lock on `file`.

Production runs `d3140a2` with expand 032 applied and the contract not run (as of the rollout that carries this runbook).

## 3. The irreversible point and what rollback means

Before the contract, nothing is irreversible:

- Removing the marker row (`DELETE FROM schema_rollout WHERE name = 'file_branch_contract';`) closes the gate again. The expand alone is safe to keep: the current code works with it, and side-branch commits to shared paths are refused, as now.

The contract's success is the irreversible point. After it:

- The old unique key is gone. Writes by the older code to `main` fail (their `ON CONFLICT` has no unique key to match). Their boot is not refused, but their writes are. Nothing lands. This is the enforcement, and the reason no old image may run again.
- Restoring the old unique key in place (`032 --rollback`, section 6) needs **no non-main file rows**. While any row has `branch <> 'main'`, the command refuses, because the pre-branch schema cannot hold such a row. Deleting those rows is a data decision for the operator, not something the command does.
- The other way back is to restore the backup taken in step 2. That returns the database to its state at the backup, so every write after the backup is lost. It is a last resort, taken with the replicas stopped.

## 4. Procedure

Run the steps in order. Stop at the first failure and follow the abort rules in section 5.

### Step 1: verify the replicas

Every replica must report a `build.git_sha` that contains `d3140a2`. The version endpoint is public and returns the build identity under `build`:

```sh
curl -fsS http://<replica>:48888/api/version
```

Check the reported `build.git_sha` against the repository (a checkout of this repository, not the replica):

```sh
git -C <checkout> merge-base --is-ancestor d3140a2 <build.git_sha> && echo "ok: <replica> runs d3140a2 or later"
```

Expected: `ok` for every replica. If `build.git_sha` is `null`, the image was built without `KOHAKU_HUB_GIT_SHA`. Do not continue until you know the image is `d3140a2` or later (for example by its build record). Any replica that runs an older image: stop it, let its in-flight requests finish, and keep it stopped. Do not restart it.

### Step 2: back up the database

```sh
mkdir -p backups
docker exec postgres pg_dump -U hub -Fc kohakuhub > backups/file-branch-pre-033-$(date -u +%Y%m%dT%H%M%SZ).dump
sha256sum backups/file-branch-pre-033-*.dump
docker exec -i postgres pg_restore --list < backups/file-branch-pre-033-<timestamp>.dump > /dev/null && echo "backup readable"
```

Expected: a non-empty dump, a checksum you record, and `backup readable`. A dump that cannot be listed is not a backup: stop.

### Step 3: read-only checks

Run the preflight. It only reads: every check runs in one read-only transaction that is never committed.

```sh
docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/contract/preflight_033.py
echo "exit=$?"
```

Expected: `exit=0`, a `PASS` line for `expand_032`, `new_key_valid`, `old_key`, `rollout_table` and `shared_path_rows`, and `preflight: all checks passed`. The notes give the file row counts on `main` and on non-main branches, and the marker state (`not set`).

Also check that nothing holds a transaction open on the database:

```sh
docker exec postgres psql -U hub -d kohakuhub -tAc \
  "SELECT pid, now() - xact_start, state FROM pg_stat_activity WHERE datname = current_database() AND xact_start < now() - interval '30 seconds'"
```

Expected: no rows. A long transaction holds back the contract's lock; wait for it to end, or abort.

### Step 4: insert the marker row

The marker is the operator's statement that step 1 holds: every replica runs `d3140a2` or later, and the old images are retired. Insert it only then:

```sh
docker exec postgres psql -U hub -d kohakuhub -v ON_ERROR_STOP=1 -c \
  "INSERT INTO schema_rollout (name, set_at) VALUES ('file_branch_contract', CURRENT_TIMESTAMP);"
```

Expected: `INSERT 0 1`. Check it with `SELECT name, set_at FROM schema_rollout;`.

### Step 5: run the contract

```sh
docker run --rm --env-file <env-file> -e PGOPTIONS='-c lock_timeout=10s' $IMAGE \
  python scripts/db_migrations/contract/033_file_branch_contract.py
echo "exit=$?"
```

Expected: `Migration 33: old key dropped, non-main File rows enabled` and `exit=0`.

The `lock_timeout` makes the contract give up, instead of queueing behind a long transaction while every other query on `file` waits. If it gives up, nothing changed (the change is one transaction); see section 5.

Running the contract a second time is harmless: it finds the guard in place and does the same thing.

### Step 6: verify

Check the index state. The new key is unique and valid; the old name is a non-unique, empty guard:

```sh
docker exec postgres psql -U hub -d kohakuhub -tAc \
  "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'file' AND indexname IN ('file_repository_id_branch_path_in_repo', 'file_repository_id_path_in_repo')"
```

Expected: the branch key is `CREATE UNIQUE INDEX ... (repository_id, branch, path_in_repo)`; the old name is `CREATE INDEX ... (repository_id, path_in_repo) WHERE false` (no `UNIQUE`).

Run the preflight again. It must now fail on `old_key` with the message that the contract already ran (the key is a non-unique guard). All other checks must pass. That failure is the expected signature of a finished contract:

```sh
docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/contract/preflight_033.py
# expected: exit=1, FAIL old_key ("is not unique: the contract already ran ..."), everything else PASS
```

Then exercise the behaviour the contract exists for, on a repository you own, through the public API:

1. A commit on a side branch that changes a path `main` has now returns `200` (before the contract it returned `409 BranchPathConflict`).
2. Reads of `main` still return `main`'s object ids (`tree`, `paths-info`, `resolve` on `main`).

### Step 7: monitor

Watch for at least 30 minutes after the contract. Any of these means something is wrong; see section 5:

```sh
# writes from an older image: the old ON CONFLICT no longer matches anything
docker logs --since 30m <replica> 2>&1 | grep -c "no unique or exclusion constraint matching the ON CONFLICT"
```

Also count the `409` responses with the `BranchPathConflict` error code in your reverse-proxy or access log. After the contract that count must stop growing: the refusal no longer happens.

Expected: `0` for both log searches on every replica that runs `d3140a2` or later.

## 5. Abort rules

| If | Do | Why |
|---|---|---|
| Step 1 finds a replica older than `d3140a2`, or a `build.git_sha` you cannot confirm | Stop that replica and keep it stopped. Do not insert the marker. | The older code writes `main` through the key the contract drops. |
| Step 2: the dump fails or is not readable | Stop. Do not continue. | No backup, no contract. |
| Step 3 exits `1` | Read the `FAIL` line. `expand_032` or `rollout_table` failing means 032 did not complete: run `docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/032_file_branch_expand.py` (it finishes the expand) and run step 3 again. `new_key_valid` failing with `INVALID` means an interrupted build: the same command rebuilds it. `shared_path_rows` failing means a broken state: stop and investigate before anything else. | The preflight refuses on purpose; the contract would refuse too, or worse. |
| Step 3: a long transaction is open | Wait for it to end, or stop the process holding it. Re-run step 3. | The contract needs a short lock on `file`. |
| Step 4 fails | Nothing changed. Re-run the insert after fixing the cause. | |
| Step 5 prints `marker row is missing` and exits `1` | The marker was not inserted (or it has another name). Insert it (step 4) and run step 5 again. | The gate is closed; nothing changed. |
| Step 5 prints `expand (032) is not applied` | Run 032 and repeat from step 3. | The contract refuses without the expand, so the old key is never the only guard. |
| Step 5 times out on the lock (`canceling statement due to lock timeout`) and exits `1` | Nothing changed. Find the long transaction (step 3), wait, and run step 5 again. | `lock_timeout` stops the queue behind it. |
| Step 5 fails for any other reason | Nothing changed: the change is one transaction. Read the message, fix the cause, run step 5 again. Do not edit the database by hand. | |
| Step 6: the index state is not as expected, or the side-branch commit does not return `200` | Stop. Compare with the backup and section 6. Do not add or drop indexes by hand. | An unexpected index state means the contract did not run as written. |
| Step 7: `ON CONFLICT` errors appear | Stop the replica that logs them immediately. Its writes fail, so no data lands through it, but its clients get errors. Find which image it is: it is an old image, and it must stay stopped. | Old images must never run again. |

## 6. Rollback

After the contract, rollback has two forms. Both are run by an operator with the backend replicas **stopped**.

**A. In place: restore the pre-branch schema.** This is the command. It drops `file.branch`, restores the old unique key, drops the marker table and the new key, and refuses while any non-main row exists:

```sh
docker run --rm --env-file <env-file> $IMAGE python scripts/db_migrations/032_file_branch_expand.py --rollback
echo "exit=$?"
```

Expected: `exit=0` and `rolled back to the pre-A schema` when every row is on `main`. While any row has `branch <> 'main'`: `exit=1` and `refusing to roll back issue #11: N file row(s) on a non-main branch ...`. Nothing changes in that case. Restoring the old key in place needs no non-main row; the command enforces it.

**B. From the backup.** Use this when non-main rows exist and you do not want to delete them. Restore the step 2 dump. It returns the database to the state at the backup: expanded, old key present, marker absent. Every write after the backup is lost.

```sh
docker exec -i postgres pg_restore -U hub --clean --if-exists -d kohakuhub < backups/file-branch-pre-033-<timestamp>.dump
```

After restoring, run the preflight. It must pass, the marker must be absent, and the old key must be unique again. Then start the replicas that run `d3140a2` or later.

Deleting non-main rows to make form A possible is a data decision (those branches' file records are lost). It is not part of this runbook.

Before the contract, no rollback is needed: section 3 covers that case.

## 7. What the operator must not do

- **Never start an old image.** Any image built before `d3140a2` (the code before per-branch rows). Its boot is not refused, but its writes fail after the contract (no unique key matches its `ON CONFLICT`). It is still a fixed copy of the old code, and it must not serve users.
- **Never run the contract without the marker row.** The script refuses without it and changes nothing. Do not insert the marker to get past a refusal you do not understand, and do not bypass the script with SQL.
- **Never run the contract from the auto-run directory.** Do not copy `033_file_branch_contract.py` into `scripts/db_migrations/`. The runner runs every top-level migration at every container start and exits non-zero when one fails: a closed gate would stop every container from starting.
- **Never re-create the old key by hand**, and never drop the guard index. Either lets an old boot restore the refusal, or restores the key for old writers.
- **Never run `032 --rollback` while the replicas serve.** Its check races with writes.
- **Never edit `schema_rollout` beyond step 4 and the abort path that removes the marker.**
- **Never run the preflight or the contract against a database you have not backed up.** The preflight writes nothing, but the contract is the step that cannot be undone in place.
