"""The backend CI cells (scripts/ci/matrix.py): what every PR runs, and what only the daily run adds."""

from __future__ import annotations

import json

import pytest

from scripts.ci import matrix

# GitHub's free plan runs 20 jobs at once and the pool is shared by every workflow of the
# organization: a pull request that needs more waits for itself.
RUNNER_POOL = 20


def _jobs(plan):
    return sum(len(cells["include"]) for cells in plan.values())


def test_a_pull_request_fits_in_one_wave_of_the_runner_pool():
    assert _jobs(matrix.plan("pull_request")) <= RUNNER_POOL


def test_a_push_to_main_runs_the_same_cells_as_a_pull_request():
    assert matrix.plan("push") == matrix.plan("pull_request")


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_the_daily_run_adds_cells_and_drops_none(event):
    pr, daily = matrix.plan("pull_request"), matrix.plan(event)
    for job, cells in pr.items():
        assert all(cell in daily[job]["include"] for cell in cells["include"])
    assert _jobs(daily) > _jobs(pr)


def test_every_full_cell_has_all_its_slices_and_no_two_cells_repeat():
    for event in ("pull_request", "schedule"):
        for job, cells in matrix.plan(event).items():
            keys = [json.dumps(cell, sort_keys=True) for cell in cells["include"]]
            assert len(keys) == len(set(keys)), job
            by_cell = {}
            for cell in cells["include"]:
                if "shard" in cell:
                    rest = {k: v for k, v in cell.items() if k != "shard"}
                    by_cell.setdefault(json.dumps(rest, sort_keys=True), set()).add(cell["shard"])
            for slices in by_cell.values():
                assert slices == {f"{i}/{matrix.SHARDS}" for i in range(1, matrix.SHARDS + 1)}


def test_a_client_only_cell_is_never_sliced():
    for cells in matrix.plan("schedule").values():
        for cell in cells["include"]:
            assert not (cell.get("subset") == "hf" and "shard" in cell)


def test_the_floor_and_the_newest_client_run_the_whole_suite_on_every_pull_request():
    full = {
        (c["python-version"], c["huggingface-hub-version"])
        for c in matrix.plan("pull_request")["main"]["include"]
        if "subset" not in c
    }
    assert ("3.10", "0.20.3") in full and ("3.12", "latest") in full


def test_the_oldest_supported_lakefs_runs_without_the_cache_on_every_pull_request():
    assert {c["lakefs"] for c in matrix.plan("pull_request")["cache"]["include"]} == {"1.48.1"}


def test_main_writes_one_output_per_job(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    matrix.main("pull_request")
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert set(lines) == set(matrix.plan("pull_request"))
    assert json.loads(lines["s3"]) == matrix.plan("pull_request")["s3"]
