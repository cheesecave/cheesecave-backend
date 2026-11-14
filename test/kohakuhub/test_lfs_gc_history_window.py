"""The history half of the LFS retention decision: which versions of a path its keep window holds (#153).

The rule is unchanged: a path keeps its newest ``keep`` unique contents, newest by ``(created_at, id)``. It used to be
decided by reading the whole history of the path and de-duplicating it in a list, quadratic in the number of versions,
for every candidate; a path with about 4,000 versions made each decision cost 60 ms and blocked the worker. Now the
newest rows are read by one statement (one snapshot) and the walk stops as soon as the answer is known; a path whose newest
rows repeat a few contents falls back to one aggregate query. Real SQL on both engines.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from kohakuhub import lfs_gc
from kohakuhub.db import LFSObjectHistory
from test.kohakuhub.support.factories import make_repo, make_user

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ABSENT = "f" * 64


def _sha(label) -> str:
    return f"{label:0>64}"[-64:]


@pytest.fixture
def repo(db_dual):
    return make_repo(make_user("gc-owner"), "history")


def _write(repo, path, versions):
    """History rows for ``versions``, a list of ``(sha, minute)``, in that insertion order."""
    rows = [
        {
            "repository": repo.id,
            "path_in_repo": path,
            "sha256": sha,
            "size": 1,
            "commit_id": f"c{i}",
            "created_at": START + timedelta(minutes=minute),
        }
        for i, (sha, minute) in enumerate(versions)
    ]
    for start in range(0, len(rows), 500):
        LFSObjectHistory.insert_many(rows[start : start + 500]).execute()


def _reference(repo, path) -> list[str]:
    """The rule as it was written before this change: unique contents, newest first."""
    H = LFSObjectHistory
    rows = H.select(H.sha256, H.created_at, H.id).where(
        (H.repository == repo.id) & (H.path_in_repo == path)
    )
    unique: list[str] = []
    for row in sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True):
        if row.sha256 not in unique:
            unique.append(row.sha256)
    return unique


def _random_history(rnd: random.Random, size: int) -> list[tuple[str, int]]:
    contents = [_sha(f"c{n}") for n in range(rnd.choice([1, 2, 3, size // 3 + 1, size]))]
    # Few distinct minutes, so many rows tie on created_at and the id decides
    return [(rnd.choice(contents), rnd.randrange(max(1, size // 4))) for _ in range(size)]


@pytest.mark.parametrize("short_walk", [False, True], ids=["default walk", "short walk"])
def test_the_window_decides_what_the_whole_history_decided(repo, monkeypatch, short_walk):
    if short_walk:  # short enough that the random histories reach the fallback
        monkeypatch.setattr(lfs_gc, "HISTORY_WALK_ROWS", 12)
    rnd = random.Random(153)
    for case in range(40):
        path = f"case/{case}.bin"
        _write(repo, path, _random_history(rnd, rnd.choice([1, 3, 9, 30, 90])))
        unique = _reference(repo, path)
        assert lfs_gc._unique_versions(repo.id, path) == unique
        for keep in (0, 1, 2, 3, 5, 10, 15):
            for sha in [*unique, ABSENT]:
                expected = sha in unique[:keep]
                assert lfs_gc._kept_by_history(repo.id, path, sha, keep) is expected, (case, keep, sha)


def test_a_history_of_distinct_versions_is_decided_from_its_newest_rows(repo):
    """The cost of a decision does not grow with the history: the case that blocked the worker."""
    for size in (200, 2_000):
        path = f"videos-{size}.parquet"
        _write(repo, path, [(_sha(f"{size}-{n}"), n) for n in range(size)])
        oldest, newest = _sha(f"{size}-0"), _sha(f"{size}-{size - 1}")

        stats = lfs_gc.HistoryStats()
        assert lfs_gc._kept_by_history(repo.id, path, oldest, 10, stats) is False
        assert (stats.paths, stats.rows, stats.fallbacks) == (1, 10, 0)  # ten newer contents, whatever the size

        stats = lfs_gc.HistoryStats()
        assert lfs_gc._kept_by_history(repo.id, path, newest, 10, stats) is True
        assert stats.rows == 1


def test_a_history_that_repeats_a_few_contents_falls_back_to_one_query(repo):
    path = "actresses.parquet"
    old = _sha("old")
    a, b = _sha("a"), _sha("b")
    _write(repo, path, [(old, 0)] + [(a if n % 2 else b, n + 1) for n in range(600)])

    stats = lfs_gc.HistoryStats()
    assert lfs_gc._kept_by_history(repo.id, path, old, 10, stats) is True  # only two newer contents
    assert (stats.rows, stats.fallbacks) == (lfs_gc.HISTORY_WALK_ROWS, 1)
    assert lfs_gc._kept_by_history(repo.id, path, old, 2, stats) is False
    assert lfs_gc._kept_by_history(repo.id, path, a, 2) is True


def test_a_short_history_is_decided_without_the_fallback(repo):
    path = "short.bin"
    _write(repo, path, [(_sha("x"), 0), (_sha("y"), 1)])
    stats = lfs_gc.HistoryStats()
    assert lfs_gc._kept_by_history(repo.id, path, ABSENT, 10, stats) is False
    assert (stats.rows, stats.fallbacks) == (2, 0)


def test_a_keep_count_of_zero_keeps_nothing(repo):
    path = "none.bin"
    _write(repo, path, [(_sha("x"), 0)])
    stats = lfs_gc.HistoryStats()
    assert lfs_gc._kept_by_history(repo.id, path, _sha("x"), 0, stats) is False
    assert (stats.paths, stats.rows) == (1, 0)


def test_the_retention_decision_reports_what_it_read(repo, monkeypatch):
    monkeypatch.setattr(lfs_gc.cfg.app, "lfs_auto_gc", True)
    repo.lfs_keep_versions = 2
    repo.save()
    shared = _sha("shared")
    _write(repo, "one.bin", [(shared, 0), (_sha("one-1"), 1), (_sha("one-2"), 2)])
    _write(repo, "two.bin", [(shared, 0), (_sha("two-1"), 1)])

    stats = lfs_gc.HistoryStats()
    assert lfs_gc.retention_reason(shared, stats=stats) == "history"  # still in two.bin's window
    assert stats.paths == 2 and stats.rows >= 4
    assert str(stats) == f"{stats.paths} path(s), {stats.rows} history row(s) read"

    assert lfs_gc.retention_reason(_sha("one-1")) == "history"
    stats = lfs_gc.HistoryStats()
    _write(repo, "one.bin", [(_sha("one-3"), 3)])
    assert lfs_gc.retention_reason(_sha("one-1"), stats=stats) is None
    assert lfs_gc.decide_delete(_sha("one-1")) is None  # nothing relies on it: tombstoned
    assert lfs_gc.decide_delete(_sha("one-3")) == "history"
    assert lfs_gc.begin_delete(_sha("one-2")) is False


def test_the_walk_reads_one_snapshot(repo, monkeypatch):
    """One statement for the newest rows: a row deleted meanwhile cannot shift what the walk sees (no OFFSET paging)."""
    statements = []
    real = lfs_gc._newest_first

    def counted(repository_id, path):
        statements.append(path)
        return real(repository_id, path)

    monkeypatch.setattr(lfs_gc, "_newest_first", counted)
    path = "snapshot.bin"
    _write(repo, path, [(_sha(f"s{n}"), n) for n in range(200)])
    assert lfs_gc._kept_by_history(repo.id, path, _sha("s0"), 150) is False
    assert statements == [path]


def test_a_fallback_is_named_in_the_report(repo, monkeypatch):
    monkeypatch.setattr(lfs_gc, "HISTORY_WALK_ROWS", 64)
    path = "loop.bin"
    _write(repo, path, [(_sha("old"), 0)] + [(_sha("a"), n + 1) for n in range(100)])
    stats = lfs_gc.HistoryStats()
    lfs_gc._kept_by_history(repo.id, path, _sha("old"), 10, stats)
    assert str(stats).endswith(", aggregate fallback 1")
