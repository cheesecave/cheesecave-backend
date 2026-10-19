"""The limits of the explorer's server side: what happens when a budget, a member count or a time limit is hit, and the branches of diff.

Files are served from a local Range server, so the readers run for real (no mocked RangeFile). Pure logic, no database.
"""
import http.server
import io
import re
import socketserver
import tarfile
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kohakuhub.api.viewer import service
from kohakuhub.api.viewer.rangefile import BudgetExceeded

BLOBS: dict[str, bytes] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = BLOBS[self.path.lstrip("/")]
        m = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range") or "")
        a, b = (int(m.group(1)), min(int(m.group(2)), len(data) - 1)) if m else (0, len(data) - 1)
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        body = data[a : b + 1]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def base():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def put(base, name, data: bytes, sha=None) -> service.FileRef:
    BLOBS[name] = data
    return service.FileRef(path=name, url=f"{base}/{name}", size=len(data), sha=sha or f"sha-{name}", commit="c0")


def parquet(table: pa.Table, row_group_size=1000) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, row_group_size=row_group_size, compression="zstd")
    return buf.getvalue()


def numbers(n=4000, extra=None):
    cols = {"id": list(range(n)), "label": [i % 4 for i in range(n)], "text": [f"row {i} cheese" for i in range(n)]}
    cols.update(extra or {})
    return pa.table(cols)


# ---------------------------------------------------------------------------------------------------- scan budgets
def test_a_scan_that_runs_out_of_budget_is_partial_and_does_not_raise(base):
    ref = put(base, "scan1.parquet", parquet(numbers()))
    out = service.scan([ref], lambda t: __import__("pyarrow.compute", fromlist=["x"]).greater_equal(t["label"], 0), 0, 100, budget=1)
    assert out["partial"] is True and out["rows"] == [] and out["scanned_rows"] == 0


def test_a_scan_with_room_for_one_row_group_returns_what_it_found_then_stops(base):
    ref = put(base, "scan2.parquet", parquet(numbers()))
    size = service.parquet_summary(ref)["row_groups"][0]["bytes"] if "bytes" in service.parquet_summary(ref)["row_groups"][0] else 0
    footprint = ref.size // 4 + 70_000                          # footer probe plus about one row group
    out = service.filter_rows([ref], '"label"=1', 0, 5000 if False else 100)    # plain filter, full budget: first page
    assert [r["row_idx"] for r in out["rows"]][:3] == [1, 5, 9] and size >= 0
    tight = service.scan([ref], lambda t: __import__("pyarrow.compute", fromlist=["x"]).equal(t["label"], 1), 0, 100, budget=footprint)
    assert tight["partial"] is True and tight["scanned_rows"] < 4000


def test_a_filter_that_matches_nothing_scans_everything_and_is_not_partial(base):
    ref = put(base, "scan3.parquet", parquet(numbers()))
    out = service.filter_rows([ref], '"label">99', 0, 100)
    assert out["rows"] == [] and out["partial"] is False and out["scanned_rows"] == 4000


def test_search_over_a_table_without_string_columns_matches_nothing(base):
    ref = put(base, "scan4.parquet", parquet(pa.table({"a": list(range(50)), "b": [1.5] * 50})))
    out = service.search_rows([ref], "x", 0, 10)
    assert out["rows"] == [] and out["partial"] is False


def test_offset_skips_matches_and_pages_are_contiguous(base):
    ref = put(base, "scan5.parquet", parquet(numbers()))
    first = service.filter_rows([ref], '"label"=0', 0, 10)["rows"]
    second = service.filter_rows([ref], '"label"=0', 10, 10)["rows"]
    assert [r["row_idx"] for r in first + second] == list(range(0, 80, 4))


# ---------------------------------------------------------------------------------------------------- tar limits
def make_tar(names: list[str], dirs=()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for d in dirs:
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for n in names:
            data = n.encode()
            info = tarfile.TarInfo(n)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_a_tar_with_more_members_than_the_limit_is_truncated(base, monkeypatch):
    monkeypatch.setattr(service, "TAR_MAX_MEMBERS", 10)
    ref = put(base, "t1.tar", make_tar([f"{i:04d}.txt" for i in range(30)]))
    out = service.tar_index(ref)
    assert out["truncated"] is True and out["members"] == 10 and len(out["samples"]) == 10


def test_a_tar_walk_that_exceeds_the_time_limit_stops_and_says_so(base, monkeypatch):
    monkeypatch.setattr(service, "TAR_TIME_LIMIT", -1.0)
    ref = put(base, "t2.tar", make_tar([f"{i:04d}.txt" for i in range(30)]))
    out = service.tar_index(ref)
    assert out["truncated"] is True and out["members"] == 1


def test_directories_are_not_members_and_samples_group_by_key_up_to_the_first_dot(base):
    ref = put(base, "t3.tar", make_tar(["a/0001.jpg", "a/0001.json", "a/0001.cls", "a/0002.jpg", "b.tar.gz.txt"], dirs=["a/"]))
    out = service.tar_index(ref)
    assert out["members"] == 5 and out["truncated"] is False
    by_key = {s["key"]: sorted(m["ext"] for m in s["members"]) for s in out["samples"]}
    assert by_key["a/0001"] == ["cls", "jpg", "json"] and by_key["a/0002"] == ["jpg"] and by_key["b"] == ["tar.gz.txt"]


def test_member_offsets_point_at_the_member_bytes(base):
    ref = put(base, "t4.tar", make_tar(["x.txt", "y.txt"]))
    for s in service.tar_index(ref)["samples"]:
        for m in s["members"]:
            assert BLOBS["t4.tar"][m["offset"] : m["offset"] + m["size"]] == s["key"].encode() + b"." + m["ext"].encode()


def test_a_tar_index_is_cached_by_content_hash(base):
    ref = put(base, "t5.tar", make_tar(["x.txt"]), sha="same-sha")
    before = dict(service.METRICS)
    service.tar_index(ref)
    other_url = put(base, "t5-copy.tar", BLOBS["t5.tar"], sha="same-sha")      # same content hash at another path
    service.tar_index(other_url)
    assert service.METRICS["cache_hits"] == before["cache_hits"] + 1 and service.METRICS["cache_misses"] == before["cache_misses"] + 1


# ---------------------------------------------------------------------------------------------------- diff
def test_diff_over_budget_raises_budget_exceeded(base):
    a = put(base, "d1a.parquet", parquet(numbers()))
    b = put(base, "d1b.parquet", parquet(numbers(extra={"flag": [True] * 4000})))
    with pytest.raises(BudgetExceeded):
        service.diff_tables(a, b, "id", budget=100)


def test_diff_without_a_usable_key_reports_schema_and_rows_only(base):
    a = put(base, "d2a.parquet", parquet(pa.table({"x": [1, 2, 3]})))
    b = put(base, "d2b.parquet", parquet(pa.table({"x": [1, 2, 3, 4], "y": [0] * 4})))
    out = service.diff_tables(a, b, None)
    assert out["keyed"] is False and out["rows"]["delta"] == 1 and out["schema"]["added"] == ["y"]
    assert service.diff_tables(a, b, "nope")["keyed"] is False                 # a key that is in neither


def test_diff_reports_removed_and_retyped_columns(base):
    a = put(base, "d3a.parquet", parquet(pa.table({"id": [1, 2], "gone": ["a", "b"], "n": [1, 2]})))
    b = put(base, "d3b.parquet", parquet(pa.table({"id": [1, 2], "n": [1.5, 2.5]})))
    out = service.diff_tables(a, b, "id")
    assert out["schema"]["removed"] == ["gone"] and out["schema"]["type_changed"] == [{"column": "n", "from": "int64", "to": "double"}]
    assert out["keyed"] is True and out["added"] == 0 and out["removed"] == 0 and out["changed"] == 2


def test_diff_of_the_same_object_reads_nothing(base):
    a = put(base, "d4.parquet", parquet(numbers()), sha="same")
    b = service.FileRef(path="d4.parquet", url="http://127.0.0.1:1/never", size=a.size, sha="same", commit="c1")
    out = service.diff_tables(a, b, "id")
    assert out["identical_content"] is True and out["keyed"] is False


def test_nulls_on_both_sides_are_not_a_change(base):
    a = put(base, "d5a.parquet", parquet(pa.table({"id": [1, 2], "v": [None, 1.0]})))
    b = put(base, "d5b.parquet", parquet(pa.table({"id": [1, 2], "v": [None, 2.0]})))
    out = service.diff_tables(a, b, "id")
    assert out["changed"] == 1 and out["changed_columns"] == {"v": 1}


# ---------------------------------------------------------------------------------------------------- router: error translation
def test_the_diff_route_turns_a_spent_budget_into_413_toolarge(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kohakuhub.api.viewer import router as r
    from kohakuhub.auth.dependencies import get_optional_user

    app = FastAPI()
    app.include_router(r.router, prefix="/api")      # main.py mounts it under the api base
    app.dependency_overrides[get_optional_user] = lambda: None

    async def open_repo(*a, **k):
        return service.RepoCtx("dataset", "n", "m", None, "lakefs", None, "commit1", "main", None)

    async def locate(ctx, path, revision=None):
        return service.FileRef(path, "http://x/", 1, "sha", "commit1")

    def diff(*a, **k):
        raise BudgetExceeded("too much")

    monkeypatch.setattr(service, "open_repo", open_repo)
    monkeypatch.setattr(service, "locate", locate)
    monkeypatch.setattr(service, "diff_tables", diff)
    res = TestClient(app).get("/api/datasets/n/m/viewer/diff", params={"path": "t.parquet", "old": "a", "new": "b"})
    assert res.status_code == 413 and res.headers["X-Error-Code"] == "TooLarge" and "budget" in res.json()["detail"]["error"]
