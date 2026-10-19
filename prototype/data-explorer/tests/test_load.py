"""Concurrency: many clients at once get the same answers they would get alone, with no errors and bounded latency.

Answers are compared with a sequential run of the same request. usage as test_api_live.py
"""
import concurrent.futures as cf
import json
import os
import statistics
import time

import httpx

BASE = os.environ.get("EXPLORER_BASE", "http://127.0.0.1:48999")
TRUTH = json.load(open(os.environ.get("EXPLORER_DATA", "/tmp/cc-proto/data") + "/truth.json"))
V = TRUTH["versions"]

REQUESTS = [
    ("/datasets/proto/reviews/viewer/rows", {"split": "train", "offset": 12345, "length": 100}),
    ("/datasets/proto/reviews/viewer/rows", {"split": "test", "offset": 0, "length": 100}),
    ("/datasets/proto/reviews/viewer/filter", {"split": "train", "where": '"label"=2', "length": 50}),
    ("/datasets/proto/reviews/viewer/search", {"split": "test", "query": "cheese", "length": 50}),
    ("/datasets/proto/reviews/viewer/statistics", {"split": "train"}),
    ("/datasets/proto/webds/viewer/tar-index", {"path": "shards/shard-0001.tar", "limit": 20}),
    ("/datasets/proto/versions/viewer/diff", {"path": "table.parquet", "old": V["v1"], "new": V["v2"], "key": "id"}),
    ("/datasets/proto/mixed-formats/viewer/rows", {"split": "train", "length": 100}),
]
WORKERS, ROUNDS = 16, 6


def strip(j):
    """Drop what legitimately differs between calls: the cost of a cold cache."""
    j = dict(j)
    for k in ("cost", "requests", "bytes"):
        j.pop(k, None)
    return j


def test_sixteen_clients_get_the_answers_they_would_get_alone():
    http = httpx.Client(trust_env=False, timeout=120, limits=httpx.Limits(max_connections=64))
    alone = {i: strip(http.get(BASE + "/api" + p, params=q).json()) for i, (p, q) in enumerate(REQUESTS)}
    jobs = [i for _ in range(ROUNDS) for i in range(len(REQUESTS))] * 2
    lat, bad = [], []

    def one(i):
        p, q = REQUESTS[i]
        t = time.perf_counter()
        r = http.get(BASE + "/api" + p, params=q)
        lat.append((time.perf_counter() - t) * 1000)
        if r.status_code != 200 or strip(r.json()) != alone[i]:
            bad.append((i, r.status_code))

    t_seq = time.perf_counter()
    for i in jobs[: len(REQUESTS) * 2]:
        http.get(BASE + "/api" + REQUESTS[i][0], params=REQUESTS[i][1])
    seq_rate = len(REQUESTS) * 2 / (time.perf_counter() - t_seq)

    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(one, jobs))
    wall = time.perf_counter() - t0
    lat.sort()
    print(f"\none client: {seq_rate:.0f} req/s; {WORKERS} clients: {len(jobs) / wall:.0f} req/s (x{len(jobs) / wall / seq_rate:.1f})")
    print(f"{len(jobs)} requests, {WORKERS} clients, {wall:.1f} s wall, {len(jobs) / wall:.0f} req/s, "
          f"p50 {statistics.median(lat):.0f} ms, p95 {lat[int(len(lat) * .95)]:.0f} ms, max {lat[-1]:.0f} ms, wrong {len(bad)}")
    assert bad == []
    assert lat[int(len(lat) * .95)] < 10_000
