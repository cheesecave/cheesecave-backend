"""The cells of the "Backend tests" workflow, one JSON matrix per job.

A pull request (and a push to main) runs the cells below in a single wave of the
organization's runner pool (20 jobs at once on the free plan; test_ci_matrix.py checks the
count). The daily run and a manual dispatch add the cells that only guard against drift.

* ``full`` cells run the whole suite, cut into SHARDS slices of whole files (shard.py).
* ``subset: hf`` cells run only the tests marked ``hf_client`` (the ones that call
  huggingface_hub), so every client version is covered without repeating the other 80%
  of the suite that does not care which client is installed.
"""

from __future__ import annotations

import json
import os
import sys

SHARDS = 3
FLOOR = ("3.10", "0.20.3")  # the oldest Python and huggingface_hub the project promises


def _sliced(cell: dict) -> list[dict]:
    return [{**cell, "shard": f"{i}/{SHARDS}"} for i in range(1, SHARDS + 1)]


def _client(python: str, version: str) -> dict:
    return {"python-version": python, "huggingface-hub-version": version, "subset": "hf"}


def plan(event: str) -> dict[str, dict]:
    daily = event in ("schedule", "workflow_dispatch")
    # Each Python runs the whole suite, with a different client: oldest, first 1.x, newest.
    main = [c for py, hf in (("3.12", "latest"), FLOOR, ("3.11", "1.0.1")) for c in _sliced({"python-version": py, "huggingface-hub-version": hf})]
    main += [_client("3.12", v) for v in ("0.36.2", "2.0.0")]
    cache = _sliced({"lakefs": "1.48.1"})  # the oldest supported LakeFS, cache off
    sqlite = _sliced({"python-version": "3.12"})
    if daily:
        main += [_client("3.12", v) for v in ("0.30.2", "1.6.0")] + [_client(py, "latest") for py in ("3.10", "3.11")]
        cache += _sliced({"lakefs": "1.86.0"})
        sqlite += _sliced({"python-version": "3.10"})
    return {
        "main": {"include": main},
        "cache": {"include": cache},
        "s3": {"include": _sliced({})},
        "sqlite": {"include": sqlite},
    }


def main(event: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        for job, cells in plan(event).items():
            out.write(f"{job}={json.dumps(cells, separators=(',', ':'))}\n")


if __name__ == "__main__":
    main(sys.argv[1])
