"""Split one pytest run into N slices of whole files, so CI can run one slice per job.

    pytest test/kohakuhub -p scripts.ci.shard --shard 2/3

A file is never split: the backend fixtures restore the shared storage whenever the
module changes, so the file is the unit. Slices are balanced by the seconds in
shard_durations.json (a file the table does not know yet counts as the median, so a
new test file lands in some slice until the table is refreshed):

    python -m scripts.ci.shard junit.xml > scripts/ci/shard_durations.json

The slicing runs after collection and after ``-m``, so ``-m hf_client --shard 1/2``
splits the marked files only.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pytest

DURATIONS = Path(__file__).with_name("shard_durations.json")


def parse_spec(spec: str) -> tuple[int, int]:
    """``"2/3"`` -> (1, 3): the zero-based slice and the slice count."""
    try:
        index, count = (int(part) for part in spec.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--shard wants INDEX/COUNT, got {spec!r}") from None
    if not 1 <= index <= count:
        raise pytest.UsageError(f"--shard {spec!r}: INDEX must be between 1 and COUNT")
    return index - 1, count


def assign(durations: dict[str, float], files: list[str], count: int) -> dict[str, int]:
    """Longest file first onto the lightest slice; the same answer in any input order."""
    median = statistics.median(durations.values()) if durations else 1.0
    load = [0.0] * count
    owner: dict[str, int] = {}
    for name in sorted(files, key=lambda f: (-durations.get(f, median), f)):
        lightest = min(range(count), key=lambda k: (load[k], k))
        owner[name] = lightest
        load[lightest] += durations.get(name, median)
    return owner


def pytest_addoption(parser):
    parser.addoption("--shard", default=None, metavar="INDEX/COUNT", help="run only slice INDEX of COUNT (CI)")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if spec is None:
        return
    index, count = parse_spec(spec)
    by_file = defaultdict(list)
    for item in items:
        by_file[item.nodeid.split("::")[0]].append(item)
    owner = assign(json.loads(DURATIONS.read_text()), list(by_file), count)
    kept = [i for name, group in by_file.items() if owner[name] == index for i in group]
    dropped = [i for name, group in by_file.items() if owner[name] != index for i in group]
    items[:] = [i for i in items if i in kept]
    config.hook.pytest_deselected(items=dropped)
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        files = sum(1 for name in by_file if owner[name] == index)
        reporter.write_line(f"[shard] {spec}: {files} of {len(by_file)} files, {len(kept)} of {len(kept) + len(dropped)} tests")


def durations_from_junit(path) -> dict[str, float]:
    """Seconds per test file from a ``--junitxml`` report (setup and teardown included)."""
    import xml.etree.ElementTree as ET

    seconds: dict[str, float] = defaultdict(float)
    for case in ET.parse(path).getroot().iter("testcase"):
        parts = case.get("classname").split(".")
        while parts and not Path("/".join(parts) + ".py").exists():
            parts.pop()  # the trailing parts name a class
        if parts:
            seconds["/".join(parts) + ".py"] += float(case.get("time"))
    return dict(seconds)


def main(argv=None) -> None:
    table = durations_from_junit((argv or sys.argv[1:])[0])
    print(json.dumps({k: round(v, 1) for k, v in sorted(table.items())}, indent=1))


if __name__ == "__main__":
    main()
