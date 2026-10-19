"""Numbers for the design issue: latency, storage Range requests and bytes of each explorer call (cold = first call after a backend restart,
warm = median of 15 repeats). Prints a markdown table. usage: python measure.py   (backend and seed data must be up)"""
import json
import os
import statistics
import time

import httpx

BASE = os.environ.get("EXPLORER_BASE", "http://127.0.0.1:48999")
TRUTH = json.load(open(os.environ.get("EXPLORER_DATA", "/tmp/cc-proto/data") + "/truth.json"))
http = httpx.Client(trust_env=False, timeout=120)
v = TRUTH["versions"]

CALLS = [
    ("splits (README yaml)", "/datasets/proto/reviews/viewer/splits", {}),
    ("size (footers)", "/datasets/proto/reviews/viewer/size", {}),
    ("rows page 0 (100 rows)", "/datasets/proto/reviews/viewer/rows", {"split": "train", "offset": 0, "length": 100}),
    ("rows page deep (row 25 000)", "/datasets/proto/reviews/viewer/rows", {"split": "train", "offset": 25000, "length": 100}),
    ("filter `label=4 AND score>3.5` (first 100)", "/datasets/proto/reviews/viewer/filter", {"split": "train", "where": '"label"=4 AND "score">3.5', "length": 100}),
    ("search `cheese cave` (first 100)", "/datasets/proto/reviews/viewer/search", {"split": "train", "query": "cheese cave", "length": 100}),
    ("statistics (40 000 rows)", "/datasets/proto/reviews/viewer/statistics", {"split": "train"}),
    ("tar index, 600 small members (880 KiB)", "/datasets/proto/webds/viewer/tar-index", {"path": "shards/shard-0000.tar"}),
    ("tar index, 20 large members (5 MiB)", "/datasets/proto/webds/viewer/tar-index", {"path": "shards/big-0000.tar"}),
    ("diff between two commits (keyed)", "/datasets/proto/versions/viewer/diff", {"path": "table.parquet", "old": v["v1"], "new": v["v2"], "key": "id"}),
    ("csv page", "/datasets/proto/mixed-formats/viewer/rows", {"split": "test", "length": 100}),
    ("jsonl page", "/datasets/proto/mixed-formats/viewer/rows", {"split": "train", "length": 100}),
]


def once(route, params):
    t = time.perf_counter()
    r = http.get(BASE + "/api" + route, params=params)
    ms = (time.perf_counter() - t) * 1000
    r.raise_for_status()
    j = r.json()
    cost = j.get("cost") or {"range_requests": j.get("requests"), "bytes_read": j.get("bytes")}
    return ms, cost


print("| call | cold ms | warm ms (p50) | storage range requests (cold) | bytes read from storage (cold) | answer size |")
print("|---|---:|---:|---:|---:|---:|")
for name, route, params in CALLS:
    cold, cost = once(route, params)
    warm = statistics.median(once(route, params)[0] for _ in range(15))
    size = len(http.get(BASE + "/api" + route, params=params).content)
    rr, bb = cost.get("range_requests"), cost.get("bytes_read")
    print(f"| {name} | {cold:.0f} | {warm:.0f} | {rr if rr is not None else 'n/a'} | {int(bb) // 1024 if bb is not None else 'n/a'} KiB | {size / 1024:.1f} KiB |")
