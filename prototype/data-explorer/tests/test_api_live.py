"""Live tests of the explorer's server side against the running prototype backend and the seeded synthetic datasets.

Ground truth never comes from the code under test: the shards are downloaded whole through /resolve and read with
pyarrow, and the explorer's Range-read answers are compared to that.
usage: EXPLORER_BASE=http://127.0.0.1:48999 EXPLORER_DATA=/tmp/cc-proto/data pytest test_api_live.py
"""
import io
import json
import os
import subprocess
import sys

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

BASE = os.environ.get("EXPLORER_BASE", "http://127.0.0.1:48999")
DATA = os.environ.get("EXPLORER_DATA", "/tmp/cc-proto/data")
TRUTH = json.load(open(f"{DATA}/truth.json"))
TOKENS = json.load(open(f"{DATA}/tokens.json"))
API = BASE + "/api"
http = httpx.Client(trust_env=False, timeout=120, follow_redirects=True)


def get(route, /, token=None, **params):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return http.get(API + route, params=params, headers=headers)


def download(repo, path, revision="main"):
    r = http.get(f"{BASE}/datasets/{repo}/resolve/{revision}/{path}")
    r.raise_for_status()
    return r.content


@pytest.fixture(scope="module")
def train_truth():
    parts = [pq.read_table(io.BytesIO(download("proto/reviews", f"data/train-0000{i}-of-00002.parquet"))) for i in (0, 1)]
    return pa.concat_tables(parts)


# ------------------------------------------------------------------------------------------------ splits and shapes
def test_splits_from_yaml_and_from_names():
    y = get("/datasets/proto/reviews/viewer/splits").json()
    assert y["source"] == "yaml"
    assert {s["split"]: len(s["files"]) for s in y["configs"][0]["splits"]} == {"train": 2, "test": 1}
    n = get("/datasets/proto/mixed-formats/viewer/splits").json()
    assert n["source"] == "naming"
    assert {s["split"]: s["format"] for s in n["configs"][0]["splits"]} == {"train": "jsonl", "test": "csv"}


def test_is_valid_and_size():
    v = get("/datasets/proto/reviews/viewer/is-valid").json()
    assert v["viewer"] and v["search"] and v["filter"] and v["statistics"] and v["formats"] == ["parquet"]
    assert get("/datasets/proto/webds/viewer/is-valid").json()["archives"] is True
    s = get("/datasets/proto/reviews/viewer/size").json()
    assert {(e["split"], e["num_rows"]) for e in s["splits"]} == {("train", 40000), ("test", 8000)}


# ------------------------------------------------------------------------------------------------ rows
def test_a_first_page_has_the_hub_shape_and_the_right_rows(train_truth):
    j = get("/datasets/proto/reviews/viewer/rows", config="default", split="train", offset=0, length=100).json()
    assert set(j) >= {"features", "rows", "num_rows_total", "num_rows_per_page", "partial", "commit"}
    assert j["num_rows_total"] == 40000 and j["num_rows_per_page"] == 100 and j["partial"] is False
    assert [r["row_idx"] for r in j["rows"]] == list(range(100))
    truth = train_truth.slice(0, 100).to_pylist()
    for got, want in zip(j["rows"], truth):
        assert got["row"]["id"] == want["id"] and got["row"]["text"] == want["text"] and got["row"]["label"] == want["label"]
    assert [f["name"] for f in j["features"]][:3] == ["id", "text", "label"]
    assert j["features"][2]["type"]["_type"] == "ClassLabel"          # the huggingface metadata of the file is honoured


@pytest.mark.parametrize("offset,length", [(0, 1), (4990, 20), (19990, 25), (39950, 100), (12345, 100)])
def test_pages_across_row_groups_and_shards_match_the_whole_file(train_truth, offset, length):
    j = get("/datasets/proto/reviews/viewer/rows", split="train", offset=offset, length=length).json()
    want = train_truth.slice(offset, length)
    assert [r["row_idx"] for r in j["rows"]] == list(range(offset, offset + want.num_rows))
    assert [r["row"]["id"] for r in j["rows"]] == want["id"].to_pylist()
    assert [r["row"]["score"] for r in j["rows"]] == want["score"].to_pylist()
    assert [r["row"]["tags"] for r in j["rows"]] == want["tags"].to_pylist()


def test_a_page_reads_few_bytes_and_a_repeat_reads_fewer():
    get("/datasets/proto/reviews/viewer/rows", split="train", offset=0, length=10)     # warm the footer cache
    j = get("/datasets/proto/reviews/viewer/rows", split="train", offset=25000, length=100).json()
    assert j["cost"]["range_requests"] <= 2
    total = sum(f["size"] for f in get("/datasets/proto/reviews/viewer/splits").json()["configs"][0]["splits"][0]["files"])
    assert j["cost"]["bytes_read"] < total * 0.25     # one row group of many, not the split


def test_limits_and_errors_have_the_hub_shape():
    r = get("/datasets/proto/reviews/viewer/rows", split="train", length=101)
    assert r.status_code == 422 and r.headers["X-Error-Code"] == "InvalidParameter"
    r = get("/datasets/proto/reviews/viewer/rows", split="nope")
    assert r.status_code == 404 and r.headers["X-Error-Code"] == "SplitNotFound"
    r = get("/datasets/proto/reviews/viewer/rows", config="nope")
    assert r.status_code == 404 and r.headers["X-Error-Code"] == "ConfigNotFound"
    assert get("/datasets/proto/does-not-exist/viewer/splits").status_code == 404
    assert get("/datasets/proto/reviews/viewer/splits", revision="no-such-branch").status_code == 404
    assert get("/datasets/proto/webds/viewer/rows", split="train").status_code == 422


def test_csv_and_jsonl_pages():
    j = get("/datasets/proto/mixed-formats/viewer/rows", split="train", offset=5990, length=20).json()
    assert j["num_rows_total"] == TRUTH["mixed"]["jsonl_rows"] and not j["partial"]
    assert [r["row"]["id"] for r in j["rows"]] == list(range(5990, 6000))
    assert isinstance(j["rows"][0]["row"]["meta"], dict)                      # a nested object stays one
    c = get("/datasets/proto/mixed-formats/viewer/rows", split="test", offset=0, length=3).json()
    assert c["num_rows_total"] == TRUTH["mixed"]["csv_rows"]
    assert [f["name"] for f in c["features"]] == ["id", "name", "value"]


def test_binary_cells_are_described_not_sent():
    j = get("/datasets/proto/images-parquet/viewer/rows", split="train", offset=0, length=3).json()
    cell = j["rows"][0]["row"]["image"]
    assert cell["_type"] == "binary-struct" and cell["path"] == "img_0000.png" and cell["len"] > 100
    assert "bytes" not in json.dumps(j["rows"][0]) or "bytes\": " not in json.dumps(j["rows"][0])


# ------------------------------------------------------------------------------------------------ filter and search
def drain(path, **params):
    """All matches by paging; returns (rows, last response)."""
    rows, off = [], 0
    while True:
        j = get(path, offset=off, length=100, **params).json()
        rows += j["rows"]
        if len(j["rows"]) < 100 and not j["partial"]:
            return rows, j
        if not j["rows"]:
            return rows, j
        off += 100


def test_filter_returns_exactly_the_matching_rows(train_truth):
    rows, last = drain("/datasets/proto/reviews/viewer/filter", split="train", where='"label"=4 AND "score">3.5')
    mask = [(l == 4 and s > 3.5) for l, s in zip(train_truth["label"].to_pylist(), train_truth["score"].to_pylist())]
    want = [i for i, m in enumerate(mask) if m]
    assert [r["row_idx"] for r in rows] == want and last["num_rows_total"] == len(want) and len(want) > 100


def test_filter_grammar_covers_or_not_parentheses_strings_and_nulls(train_truth):
    j = get("/datasets/proto/reviews/viewer/filter", split="train", where="NOT (\"label\"=0 OR \"label\"=1) AND \"id\"<=10", length=100).json()
    want = [i for i in range(11) if train_truth["label"][i].as_py() not in (0, 1)]
    assert [r["row_idx"] for r in j["rows"]] == want
    j = get("/datasets/proto/reviews/viewer/filter", split="train", where="\"text\"<>'x' AND \"id\"<5", length=100).json()
    assert [r["row_idx"] for r in j["rows"]] == [i for i in range(5) if train_truth["text"][i].as_py() is not None]   # a null never matches


@pytest.mark.parametrize("where", ['label=1', '"label"==1', '"label">', '"label"=1; DROP TABLE x', '"label"=1 OR', "('a'=1)", '"nope">1', "\"label\"='abc'", '"label"=1 UNION SELECT 1'])
def test_a_bad_where_clause_is_a_422_and_nothing_else(where):
    r = get("/datasets/proto/reviews/viewer/filter", split="train", where=where)
    assert r.status_code == 422 and r.headers["X-Error-Code"] == "InvalidFilter"


def test_search_matches_any_text_column_case_insensitively(train_truth):
    rows, last = drain("/datasets/proto/reviews/viewer/search", split="test", query="CHEESE CAVE")
    assert rows and all("cheese cave" in (r["row"]["text"] or "").lower() for r in rows)
    test = pq.read_table(io.BytesIO(download("proto/reviews", "data/test-00000-of-00001.parquet")))
    want = [i for i, t in enumerate(test["text"].to_pylist()) if t and "cheese cave" in t.lower()]
    assert [r["row_idx"] - 40000 for r in rows] == want or [r["row_idx"] for r in rows] == want


def test_a_scan_that_stops_early_says_partial_and_does_not_claim_a_total():
    j = get("/datasets/proto/reviews/viewer/filter", split="train", where='"label">=0', length=10).json()
    assert j["partial"] is True and j["num_rows_total"] is None and len(j["rows"]) == 10


# ------------------------------------------------------------------------------------------------ statistics
def test_statistics_are_exact_from_footers_and_histograms_say_what_they_sampled(train_truth):
    j = get("/datasets/proto/reviews/viewer/statistics", split="train").json()
    assert j["num_examples"] == 40000 and j["partial"] is False and j["sampled_rows"] == 40000
    by = {s["column_name"]: s for s in j["statistics"]}
    assert by["text"]["column_statistics"]["null_count"] == TRUTH["reviews"]["text_nulls_train"] and by["text"]["column_statistics"]["exact_from_footer"]
    assert by["score"]["column_statistics"]["min"] == TRUTH["reviews"]["score_min_train"] and by["score"]["column_statistics"]["max"] == TRUTH["reviews"]["score_max_train"]
    freq = by["label"]["column_statistics"]["frequencies"]
    assert [freq[n] for n in ["very negative", "negative", "neutral", "positive", "very positive"]] == TRUTH["reviews"]["label_hist_train"]
    assert by["label"]["column_type"] == "class_label" and by["score"]["column_type"] == "float" and by["text"]["column_type"] == "string_text"
    assert sum(by["score"]["column_statistics"]["histogram"]["hist"]) == 40000


# ------------------------------------------------------------------------------------------------ archives
def test_tar_index_groups_webdataset_samples_and_the_offsets_are_right():
    j = get("/datasets/proto/webds/viewer/tar-index", path="shards/shard-0000.tar", limit=5).json()
    assert j["members"] == 600 and j["num_samples"] == 200 and not j["truncated"]
    sample = j["samples"][0]
    assert sorted(m["ext"] for m in sample["members"]) == ["jpg", "json", "txt"]
    assert j["cost"]["range_requests"] <= 8                                   # headers only: never the member data
    assert j["cost"]["bytes_read"] <= j["file_size"]          # 600 tiny members: a header every ~1.4 KB, so the walk touches every block
    url =f"{BASE}/datasets/proto/webds/resolve/main/shards/shard-0000.tar"
    for m in sample["members"]:
        body = http.get(url, headers={"Range": f"bytes={m['offset']}-{m['offset'] + m['size'] - 1}"}).content
        assert len(body) == m["size"]
        if m["ext"] == "jpg":
            assert body[:3] == b"\xff\xd8\xff"
        if m["ext"] == "json":
            assert "caption" in json.loads(body)


@pytest.fixture(scope="module")
def big_member_tar():
    """5 MB of synthetic bytes in 20 members of 256 KB: the shape of real WebDataset shards (large members)."""
    import tarfile
    import random
    from huggingface_hub import HfApi
    api = HfApi(endpoint=BASE, token=TOKENS["proto"])
    if "shards/big-0000.tar" not in api.list_repo_files("proto/webds", repo_type="dataset"):
        rng, buf = random.Random(7), io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for i in range(20):
                data = rng.randbytes(256 * 1024)
                info = tarfile.TarInfo(f"s{i:03d}.bin")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        api.upload_file(path_or_fileobj=buf.getvalue(), path_in_repo="shards/big-0000.tar", repo_id="proto/webds", repo_type="dataset")
    return "shards/big-0000.tar"


def test_tar_index_of_large_members_reads_a_small_fraction(big_member_tar):
    j = get("/datasets/proto/webds/viewer/tar-index", path=big_member_tar).json()
    assert j["members"] == 20 and j["cost"]["bytes_read"] < j["file_size"] * 0.15, j["cost"]


def test_tar_index_refuses_what_is_not_a_tar_and_is_cached_by_content():
    assert get("/datasets/proto/webds/viewer/tar-index", path="README.md").status_code == 422
    before = get("/datasets/proto/webds/viewer/_metrics").json()
    get("/datasets/proto/webds/viewer/tar-index", path="shards/shard-0001.tar")
    mid = get("/datasets/proto/webds/viewer/_metrics").json()
    again = get("/datasets/proto/webds/viewer/tar-index", path="shards/shard-0001.tar").json()
    after = get("/datasets/proto/webds/viewer/_metrics").json()
    assert after["cache_hits"] > mid["cache_hits"] and after["cache_misses"] == mid["cache_misses"]
    assert again["members"] == 600


# ------------------------------------------------------------------------------------------------ revisions
def test_diff_between_two_revisions():
    t = TRUTH["versions"]
    j = get("/datasets/proto/versions/viewer/diff", path="table.parquet", old=t["v1"], new=t["v2"], key="id").json()
    assert j["rows"] == {"old": 3000, "new": t["v2_rows"], "delta": t["v2_rows"] - 3000}
    assert j["schema"]["added"] == ["flag"] and j["schema"]["removed"] == []
    assert (j["added"], j["removed"]) == (t["added"], t["removed"])
    assert j["changed"] >= t["changed"] - 2 and "value" in j["changed_columns"]       # 1 in 30 ids, minus the removed ones
    assert j["identical_content"] is False and j["keyed"] is True


def test_the_same_object_in_two_revisions_is_identical_without_reading_it():
    t = TRUTH["versions"]
    j = get("/datasets/proto/versions/viewer/diff", path="table.parquet", old=t["v1"], new=t["v1"]).json()
    assert j["identical_content"] is True and j["keyed"] is False


def test_revisions_resolve_to_a_pinned_commit():
    t = TRUTH["versions"]
    a = get("/datasets/proto/versions/viewer/rows", split="train", length=1, revision=t["v1"]).json()
    b = get("/datasets/proto/versions/viewer/rows", split="train", length=1, revision="main").json()
    assert a["num_rows_total"] == 3000 and b["num_rows_total"] == TRUTH["versions"]["v2_rows"]
    assert a["commit"] != b["commit"]


# ------------------------------------------------------------------------------------------------ Hub compatibility
def test_the_parquet_listing_has_the_hubs_shape_and_the_urls_work():
    j = get("/datasets/proto/reviews/parquet").json()
    assert set(j) == {"default"} and len(j["default"]["train"]) == 2 and len(j["default"]["test"]) == 1
    body = http.get(j["default"]["test"][0]).content          # 302 -> /resolve -> 302 -> storage
    assert pq.read_table(io.BytesIO(body)).num_rows == 8000
    assert get("/datasets/proto/reviews/parquet/default/train").json() == j["default"]["train"]
    assert get("/datasets/proto/reviews/parquet/default/nope").status_code == 404


def test_the_real_hf_cli_refuses_third_party_endpoints_before_asking():
    """Finding: `hf datasets parquet` is gated client-side to huggingface.co, so /parquet is for libraries and plain HTTP."""
    env = {**os.environ, "HF_ENDPOINT": BASE, "NO_PROXY": "127.0.0.1"}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    out = subprocess.run([os.path.join(os.path.dirname(sys.executable), "hf"), "datasets", "parquet", "proto/reviews"], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode != 0 and "third-party endpoints" in (out.stdout + out.stderr)


# ------------------------------------------------------------------------------------------------ permissions
def test_a_private_repository_is_not_visible_to_a_stranger_and_is_to_its_owner():
    path = "/datasets/proto/private-data/viewer/rows"
    assert get(path, split="train").status_code in (401, 404)
    assert get(path, split="train", token=TOKENS["other"]).status_code == 404         # as for a missing repository
    mine = get(path, split="train", token=TOKENS["proto"], length=5)
    assert mine.status_code == 200 and mine.json()["num_rows_total"] == 500
    # the explorer does not widen access: the presigned URL it reads is never part of an answer
    assert "X-Amz" not in mine.text and "127.0.0.1:49102" not in mine.text
    for route in ("splits", "is-valid", "size"):
        assert get(f"/datasets/proto/private-data/viewer/{route}").status_code in (401, 404)
        assert get(f"/datasets/proto/private-data/viewer/{route}", token=TOKENS["proto"]).status_code == 200
    assert get("/datasets/proto/private-data/parquet").status_code in (401, 404)


def test_the_cache_is_keyed_by_content_not_by_revision():
    t = TRUTH["versions"]
    get("/datasets/proto/reviews/viewer/rows", split="test", length=1)      # fill
    commit = get("/datasets/proto/reviews/viewer/splits").json()["commit"]
    before = get("/datasets/proto/reviews/viewer/_metrics").json()
    get("/datasets/proto/reviews/viewer/rows", split="test", length=1, revision=commit)   # same object, revision spelled as a commit id
    after = get("/datasets/proto/reviews/viewer/_metrics").json()
    assert after["cache_misses"] == before["cache_misses"] and after["cache_hits"] > before["cache_hits"]
