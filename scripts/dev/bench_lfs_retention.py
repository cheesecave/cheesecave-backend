#!/usr/bin/env python3
"""Load test of the LFS retention decision and collection (#153): a performance curve, old code against new.

Runs against an EMPTY PostgreSQL database of its own (never a real one: ``seed`` fills it with synthetic history).
The same data serves both versions; run the measuring commands once with this checkout's ``src`` on PYTHONPATH and
once with ``main``'s, then ``plot``. Only APIs both versions have are used (``lfs_gc.retention_reason``,
``storage_cleanup.collect_lfs``, ``worker.Worker``). Storage deletes are skipped (no bucket is involved).

    export KOHAKU_HUB_DB_BACKEND=postgres KOHAKU_HUB_DATABASE_URL=postgresql://...  KOHAKU_HUB_LFS_AUTO_GC=true
    PYTHONPATH=src python scripts/dev/bench_lfs_retention.py seed
    PYTHONPATH=src python scripts/dev/bench_lfs_retention.py index new         # or: index old (main's index)
    PYTHONPATH=src python scripts/dev/bench_lfs_retention.py measure --label new --out new.json
    PYTHONPATH=<main>/src python scripts/dev/bench_lfs_retention.py measure --label old --out old.json --budget 10
    python scripts/dev/bench_lfs_retention.py plot old.json new.json --out-dir pictures/

``measure`` runs: the per-candidate decision on paths of 1k..256k rows in three shapes; a content referenced from
1, 10 and 139 paths; whole collections of 1k, 10k and 100k candidates (time, event-loop lag, log lines); and a real
worker (lease 60 s, concurrency 4) with four collections and nine small tasks queued (lease losses, waits).
"""

import argparse
import asyncio
import json
import statistics
import sys
import time

SIZES = [1_000, 4_000, 16_000, 64_000, 256_000]
SHAPES = {"distinct": "every row a new content", "two": "two contents alternating, one old content at the bottom",
          "mixed": "contents drawn at random from a quarter as many"}
COLLECTIONS = [1_000, 10_000, 100_000]
REFS = [1, 10, 139]
FILLER_ROWS = 1_500_000
KEEP = 10
OWNER = "bench-owner"


def _db():
    from kohakuhub.db import db

    return db


def _sql_sha(expr: str) -> str:
    return f"md5({expr}) || md5('x' || {expr})"


def seed() -> None:
    from kohakuhub.db import Repository, User, init_db

    init_db()
    db = _db()
    owner = User.create(username=OWNER, normalized_name=OWNER, is_org=True)
    repo = Repository.create(repo_type="dataset", namespace=OWNER, name="history", full_id=f"{OWNER}/history",
                             owner=owner)
    r = repo.id
    sha, ago = _sql_sha, "now() - ({} || ' minutes')::interval".format
    parts = ["SELECT 'filler/' || mod(g, 300000), " + sha("'f' || g") + ", now() - (g || ' seconds')::interval "
             f"FROM generate_series(1, {FILLER_ROWS}) g"]
    for h in SIZES:
        series = f" FROM generate_series(1, {h}) g"
        parts.append(f"SELECT 'd{h}', " + sha(f"'d{h}-' || g") + ", " + ago(f"{h} - g") + series)
        parts.append(f"SELECT 'two{h}', CASE WHEN g = 1 THEN " + sha(f"'old{h}'") + " ELSE "
                     + sha("'ab' || mod(g, 2)") + " END, " + ago(f"{h} - g") + series)
        parts.append(f"SELECT 'mixed{h}', CASE WHEN g = 1 THEN " + sha(f"'oldm{h}'") + " ELSE "
                     + sha(f"'m{h}-' || floor(random() * {h // 4})") + " END, " + ago(f"{h} - g") + series)
    for c in COLLECTIONS:  # a path whose oldest c versions are out of the window
        parts.append(f"SELECT 'c{c}', " + sha(f"'c{c}-' || g") + ", " + ago(f"{c + KEEP} - g")
                     + f" FROM generate_series(1, {c + KEEP}) g")
    for n in REFS:  # one content, the oldest version of n paths of 1,000 versions each
        parts.append(f"SELECT 'refs{n}/' || p, CASE WHEN g = 1 THEN " + sha(f"'shared{n}'") + " ELSE "
                     + sha(f"'r{n}-' || p || '-' || g") + " END, " + ago("1000 - g")
                     + f" FROM generate_series(1, {n}) p, generate_series(1, 1000) g")
    started = time.time()
    db.execute_sql(
        "INSERT INTO lfsobjecthistory (repository_id, path_in_repo, sha256, size, commit_id, created_at) "
        f"SELECT {r}, path, sha, 1, 'bench', at FROM (" + " UNION ALL ".join(parts) + ") s(path, sha, at) "
        "ORDER BY random()"  # rows of one path end up scattered over the table, as in production
    )
    db.execute_sql("ANALYZE lfsobjecthistory")
    rows = db.execute_sql("SELECT count(*) FROM lfsobjecthistory").fetchone()[0]
    print(f"seeded {rows} history rows in {time.time() - started:.0f}s")


NEW_INDEX = "lfsobjecthistory_repository_id_path_in_repo_created_at_id"
OLD_INDEX = "lfsobjecthistory_repository_id_path_in_repo"


def index(which: str) -> None:
    db = _db()
    db.execute_sql(f'DROP INDEX IF EXISTS "{NEW_INDEX}"')
    db.execute_sql(f'DROP INDEX IF EXISTS "{OLD_INDEX}"')
    started = time.time()
    if which == "new":
        db.execute_sql(f'CREATE INDEX "{NEW_INDEX}" ON lfsobjecthistory (repository_id, path_in_repo, created_at, id)')
    else:
        db.execute_sql(f'CREATE INDEX "{OLD_INDEX}" ON lfsobjecthistory (repository_id, path_in_repo)')
    took = time.time() - started
    size = db.execute_sql(
        f"SELECT pg_size_pretty(pg_relation_size('{NEW_INDEX if which == 'new' else OLD_INDEX}'))").fetchone()[0]
    db.execute_sql("ANALYZE lfsobjecthistory")
    rows = db.execute_sql("SELECT count(*) FROM lfsobjecthistory").fetchone()[0]
    print(json.dumps({"index": which, "rows": rows, "build_seconds": round(took, 2), "size": size}))


def _oldest(path: str) -> str:
    return _db().execute_sql(
        "SELECT sha256 FROM lfsobjecthistory WHERE path_in_repo = %s ORDER BY created_at, id LIMIT 1", (path,)
    ).fetchone()[0]


def _time_decision(sha: str, reps: int) -> float:
    from kohakuhub import lfs_gc

    times = []
    for _ in range(reps):
        started = time.perf_counter()
        lfs_gc.retention_reason(sha)
        times.append((time.perf_counter() - started) * 1000)
    return statistics.median(times)


def _decisions(budget: float) -> list[dict]:
    out = []
    for shape in SHAPES:
        for h in SIZES:
            path = {"distinct": f"d{h}", "two": f"two{h}", "mixed": f"mixed{h}"}[shape]
            sha = _oldest(path)
            first = _time_decision(sha, 1)
            ms = first if first > budget * 1000 / 5 else _time_decision(sha, 5 if h < 64_000 else 3)
            out.append({"shape": shape, "rows": h, "ms": round(ms, 3)})
            print(f"  decision {shape:8} {h:>7} rows: {ms:10.2f} ms", flush=True)
            if ms * 16 > budget * 1000:  # the next size, 4x the rows, could be 16x slower
                print(f"  (stopping {shape}: the next size could exceed the {budget:g} s budget)")
                break
    return out


def _refs() -> list[dict]:
    out = []
    for n in REFS:
        ms = _time_decision(_oldest(f"refs{n}/1"), 3)
        out.append({"paths": n, "ms": round(ms, 3)})
        print(f"  decision for a content in {n:>3} path(s): {ms:8.2f} ms", flush=True)
    return out


def _reset_candidates(shas: list[str]) -> None:
    from kohakuhub import lfs_gc
    from kohakuhub.db import LfsGcCandidate, LfsObjectTombstone

    LfsGcCandidate.delete().execute()
    LfsObjectTombstone.delete().execute()
    lfs_gc.record_candidates(shas)
    lfs_gc.mark_references_reconciled()


def _evicted(c: int) -> list[str]:
    return [row[0] for row in _db().execute_sql(
        "SELECT sha256 FROM lfsobjecthistory WHERE path_in_repo = %s ORDER BY created_at, id LIMIT %s", (f"c{c}", c))]


class _Ticker:
    """How late a 100 ms sleep on the event loop wakes up: the longest stall of the loop."""

    def __init__(self):
        self.max_lag = 0.0
        self._task = None

    async def _run(self):
        while True:
            started = time.perf_counter()
            await asyncio.sleep(0.1)
            self.max_lag = max(self.max_lag, time.perf_counter() - started - 0.1)

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


async def _collections(budget: float) -> list[dict]:
    from kohakuhub import storage_cleanup, tasks
    from kohakuhub.task_testing import RecordingContext

    storage_cleanup._delete_keys = lambda bucket, keys: None
    tasks.install_log_capture()
    out = []
    for c in COLLECTIONS:
        _reset_candidates(_evicted(c))
        ctx = RecordingContext()
        ticker = _Ticker()
        ticker.start()
        token = tasks.current_context.set(ctx)
        started = time.perf_counter()
        run = asyncio.create_task(storage_cleanup.collect_lfs({}, ctx))
        done, _ = await asyncio.wait({run}, timeout=budget * 60)
        took = time.perf_counter() - started
        if not done:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        tasks.current_context.reset(token)
        await ticker.stop()
        logs = ctx.take_logs()
        checked = ctx.reports[-1][0] if ctx.reports else 0
        out.append({"candidates": c, "seconds": round(took, 2), "finished": bool(done), "checked": checked,
                    "max_loop_lag_s": round(ticker.max_lag, 3), "log_lines": len(logs),
                    "log_bytes": sum(len(e["message"]) for e in logs)})
        print(f"  collection of {c:>7}: {took:8.1f}s {'finished' if done else 'STOPPED at the budget'}, "
              f"checked {checked}, loop stalled up to {ticker.max_lag:.2f}s, {len(logs)} log line(s)", flush=True)
        if not done:
            break
    return out


async def _worker(budget: float, candidates: int) -> dict:
    from kohakuhub import storage_cleanup, tasks
    from kohakuhub.db import BackgroundTask, BackgroundTaskEvent
    from kohakuhub.worker import Worker

    storage_cleanup._delete_keys = lambda bucket, keys: None
    BackgroundTask.delete().execute()
    _reset_candidates(_evicted(candidates))
    waits = []

    @tasks.task("bench.small")
    async def small(payload):
        waits.append(time.time() - payload["queued_at"])

    for _ in range(4):  # as in production: four collections at once
        tasks.enqueue(storage_cleanup.COLLECT_LFS_KIND)
    for _ in range(9):
        tasks.enqueue("bench.small", {"queued_at": time.time()})
    worker = Worker(worker_id="bench", concurrency=4, lease_seconds=60, poll_interval=0.5, queues=[])
    ticker = _Ticker()
    ticker.start()
    stop = asyncio.Event()
    started = time.perf_counter()
    runner = asyncio.create_task(worker.run(stop))
    T = BackgroundTask
    collecting = (T.kind == storage_cleanup.COLLECT_LFS_KIND) & T.status.in_([tasks.QUEUED, tasks.RUNNING])
    collections_done = None
    while time.perf_counter() - started < budget * 60:  # periodic tasks always have a next one queued: not waited for
        await asyncio.sleep(1)
        if collections_done is None and not T.select().where(collecting).exists():
            collections_done = time.perf_counter() - started
        if collections_done is not None and len(waits) == 9:
            break
    took = time.perf_counter() - started
    stop.set()
    await runner
    await ticker.stop()
    E = BackgroundTaskEvent
    statuses = {s: n for s, n in _db().execute_sql("SELECT status, count(*) FROM background_task GROUP BY 1")}
    result = {"candidates": candidates, "seconds": round(took, 1),
              "collections_done_s": None if collections_done is None else round(collections_done, 1),
              "collections_failed": T.select().where((T.kind == storage_cleanup.COLLECT_LFS_KIND)
                                                     & (T.status == tasks.FAILED)).count(),
              "lease_expired": E.select().where(E.type == "lease_expired").count(),
              "statuses": statuses, "max_loop_lag_s": round(ticker.max_lag, 2),
              "small_tasks_done": len(waits), "small_task_wait_max_s": round(max(waits), 1) if waits else None}
    print(f"  worker: {json.dumps(result)}", flush=True)
    return result


def measure(label: str, out_path: str, budget: float, worker_candidates: int, parts: list[str]) -> None:
    import os

    from kohakuhub.config import cfg

    cfg.app.lfs_auto_gc = True
    cfg.app.lfs_keep_versions = KEEP
    result = json.load(open(out_path)) if os.path.exists(out_path) else {}
    result["label"] = label
    if "decisions" in parts:
        print(f"[{label}] per-candidate decisions (budget {budget:g} s each)")
        result["decisions"], result["refs"] = _decisions(budget), _refs()
    if "collections" in parts:
        print(f"[{label}] whole collections (budget {budget:g} min each)")
        result["collections"] = asyncio.run(_collections(budget))
    if "worker" in parts:
        print(f"[{label}] a real worker, four collections and nine small tasks (budget {budget:g} min)")
        result["worker"] = asyncio.run(_worker(budget, worker_candidates))
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1)


def plot(paths: list[str], out_dir: str) -> None:
    import os

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    runs = [json.load(open(p)) for p in paths]
    os.makedirs(out_dir, exist_ok=True)
    styles = {"distinct": "o-", "two": "s--", "mixed": "^:"}
    fig, ax = plt.subplots(figsize=(8, 5))
    for run in runs:
        for shape in SHAPES:
            pts = [d for d in run["decisions"] if d["shape"] == shape]
            ax.plot([d["rows"] for d in pts], [d["ms"] for d in pts], styles[shape],
                    label=f"{run['label']}: {shape}")
    ax.set_xscale("log"), ax.set_yscale("log")
    ax.set_xlabel("history rows of the path"), ax.set_ylabel("time to decide one candidate (ms, median)")
    ax.set_title("LFS retention decision: cost against history length")
    ax.grid(True, which="both", alpha=0.3), ax.legend(fontsize=8)
    fig.tight_layout(), fig.savefig(f"{out_dir}/decision-curve.png", dpi=130)
    fig, ax = plt.subplots(figsize=(8, 5))
    for run in runs:
        pts = [p for p in run["collections"] if p["checked"]]
        per = [p["seconds"] * 1000 / p["checked"] for p in pts]
        line, = ax.plot([p["candidates"] for p in pts], per, "o-", label=run["label"])
        for p, y in zip(pts, per):
            if not p["finished"]:  # stopped at the budget: the time per candidate it did check
                ax.plot([p["candidates"]], [y], "x", ms=12, color=line.get_color())
                ax.annotate(f"stopped at the budget\nafter {p['checked']:,} checked", (p["candidates"], y),
                            textcoords="offset points", xytext=(-120, -10), fontsize=8)
    ax.set_xscale("log"), ax.set_yscale("log")
    ax.set_xlabel("candidates (old versions of one path with as many versions)")
    ax.set_ylabel("collection time per candidate checked (ms)")
    ax.set_title("A whole collection: cost per candidate against size")
    ax.grid(True, which="both", alpha=0.3), ax.legend()
    fig.tight_layout(), fig.savefig(f"{out_dir}/collection-curve.png", dpi=130)
    print(f"wrote {out_dir}/decision-curve.png and {out_dir}/collection-curve.png")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    p = sub.add_parser("index")
    p.add_argument("which", choices=["old", "new"])
    p = sub.add_parser("measure")
    p.add_argument("--label", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--budget", type=float, default=10.0, help="seconds per decision, minutes per collection")
    p.add_argument("--worker-candidates", type=int, default=10_000)
    p.add_argument("--parts", default="decisions,collections,worker", help="comma-separated; results merge into --out")
    p = sub.add_parser("plot")
    p.add_argument("runs", nargs="+")
    p.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "seed":
        seed()
    elif args.command == "index":
        index(args.which)
    elif args.command == "measure":
        measure(args.label, args.out, args.budget, args.worker_candidates, args.parts.split(","))
    else:
        plot(args.runs, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
