"""Optional server-side SQL (tier 2, off by default): one query in a throwaway subprocess.

The worker is the same file run as a script (`python -I sandbox.py`), so it imports nothing from the application and
holds no database or storage credentials. It sees only the files it is handed, with every other door closed:
`enable_external_access=false` (no http/S3/other files), `allowed_paths` (just the staged files), `lock_configuration`
(the query cannot undo any of it), a memory limit, one thread, and a parent that kills it at the timeout.
"""
import json
import resource
import subprocess
import sys

MAX_ROWS = 1000


def _limit_address_space(nbytes: int):
    def apply():  # runs in the child between fork and exec; DuckDB's memory_limit alone is not a hard cap (list() aggregates ignore it)
        resource.setrlimit(resource.RLIMIT_AS, (nbytes, nbytes))
    return apply


def run(sql: str, tables: dict[str, str], *, timeout: float = 10.0, memory: str = "256MB", address_space: int = 4 << 30, max_rows: int = MAX_ROWS, python: str = sys.executable) -> dict:
    """Run `sql` against `tables` ({view name: local parquet path}). Returns {"columns", "rows", "truncated"} or raises Sandboxed."""
    request = json.dumps({"sql": sql, "tables": tables, "memory": memory, "max_rows": max_rows})
    try:
        done = subprocess.run([python, "-I", __file__], input=request, capture_output=True, text=True, timeout=timeout, env={}, preexec_fn=_limit_address_space(address_space))
    except subprocess.TimeoutExpired as e:  # subprocess.run kills the child before raising
        raise Sandboxed("timeout", f"the query ran longer than {timeout:g} s and was stopped") from e
    if done.returncode != 0:
        kind = "memory" if done.returncode < 0 else "failed"  # killed by a signal: the kernel or the limit ended it
        raise Sandboxed(kind, done.stderr.strip().splitlines()[-1] if done.stderr.strip() else f"worker exited with {done.returncode}")
    out = json.loads(done.stdout)
    if "error" in out:
        raise Sandboxed(out["kind"], out["error"])
    return out


class Sandboxed(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # timeout | memory | blocked | sql | failed


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _worker():  # pragma: no cover - runs in the child; exercised through run()
    import duckdb

    req = json.load(sys.stdin)
    con = duckdb.connect(":memory:", config={"threads": "1", "memory_limit": req["memory"]})
    for name, path in req["tables"].items():
        con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet({_lit(path)})')
    con.execute("SET allowed_paths=[" + ", ".join(_lit(p) for p in req["tables"].values()) + "]")
    con.execute("SET enable_external_access=false")
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET autoload_known_extensions=false")
    con.execute("SET lock_configuration=true")
    try:
        cursor = con.execute(req["sql"])
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(req["max_rows"] + 1)
    except duckdb.Error as e:
        text = str(e)
        kind = "blocked" if any(w in text for w in ("Permission", "not allowed", "disabled", "locked", "external access", "configuration")) else "sql"
        print(json.dumps({"error": text.splitlines()[0], "kind": kind}))
        return
    truncated = len(rows) > req["max_rows"]
    print(json.dumps({"columns": columns, "rows": rows[: req["max_rows"]], "truncated": truncated}, default=str))


if __name__ == "__main__":  # pragma: no cover
    _worker()
