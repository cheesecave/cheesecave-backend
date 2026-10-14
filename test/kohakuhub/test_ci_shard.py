"""The CI shard plugin (scripts/ci/shard.py): a slice of the files, nothing lost, nothing twice."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.ci import shard


def _items(*files):
    return [SimpleNamespace(nodeid=f"{f}::test_{n}") for f in files for n in range(2)]


def test_every_file_lands_in_exactly_one_shard():
    durations = {f"t/f{i}.py": float(i + 1) for i in range(10)}
    owners = shard.assign(durations, list(durations), 3)
    assert sorted(owners) == sorted(durations)
    assert set(owners.values()) == {0, 1, 2}


def test_the_shards_are_balanced_by_seconds():
    durations = {"a.py": 10.0, "b.py": 9.0, "c.py": 5.0, "d.py": 5.0, "e.py": 1.0}
    owners = shard.assign(durations, list(durations), 2)
    load = [sum(s for f, s in durations.items() if owners[f] == k) for k in (0, 1)]
    assert max(load) - min(load) <= 1.0


def test_the_split_does_not_depend_on_the_order_the_files_come_in():
    durations = {f"f{i}.py": float(i % 4 + 1) for i in range(12)}
    files = list(durations)
    assert shard.assign(durations, files, 3) == shard.assign(durations, files[::-1], 3)


def test_a_file_the_table_does_not_know_counts_as_the_median():
    durations = {"a.py": 1.0, "b.py": 3.0, "c.py": 100.0}
    owners = shard.assign(durations, ["a.py", "b.py", "c.py", "new.py"], 2)
    assert set(owners) == {"a.py", "b.py", "c.py", "new.py"}
    # the big file is alone; the median-sized newcomer joins the small ones
    assert owners["new.py"] != owners["c.py"]


def test_an_empty_table_still_splits_the_files():
    owners = shard.assign({}, ["a.py", "b.py", "c.py"], 3)
    assert sorted(owners.values()) == [0, 1, 2]


def test_one_shard_keeps_everything():
    assert set(shard.assign({"a.py": 1.0}, ["a.py", "b.py"], 1).values()) == {0}


@pytest.mark.parametrize("spec", ["", "3", "0/3", "4/3", "a/b", "1/0", "1/3/5", "-1/3"])
def test_a_bad_spec_is_refused(spec):
    with pytest.raises(pytest.UsageError):
        shard.parse_spec(spec)


def test_a_good_spec_is_one_based():
    assert shard.parse_spec("2/3") == (1, 3)


def test_a_modifyitems_keeps_one_slice_and_reports_the_rest_as_deselected():
    items = _items("t/a.py", "t/b.py", "t/c.py")
    deselected = []
    config = SimpleNamespace(
        getoption=lambda name: "1/2",
        hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)),
        pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
    )
    shard.pytest_collection_modifyitems(config, items)
    kept_files = {i.nodeid.split("::")[0] for i in items}
    dropped_files = {i.nodeid.split("::")[0] for i in deselected}
    assert kept_files and dropped_files and not kept_files & dropped_files
    assert len(items) + len(deselected) == 6
    assert all(sum(1 for i in items if i.nodeid.startswith(f)) == 2 for f in kept_files)


def test_without_the_option_nothing_is_touched():
    items = _items("t/a.py")
    config = SimpleNamespace(getoption=lambda name: None)
    shard.pytest_collection_modifyitems(config, items)
    assert len(items) == 2


def test_the_reporter_line_names_the_slice():
    lines = []
    reporter = SimpleNamespace(write_line=lines.append)
    config = SimpleNamespace(
        getoption=lambda name: "2/2",
        hook=SimpleNamespace(pytest_deselected=lambda items: None),
        pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter),
    )
    shard.pytest_collection_modifyitems(config, _items("t/a.py", "t/b.py"))
    assert lines and "2/2" in lines[0]


def test_the_table_that_ships_with_the_repo_is_a_plain_file_to_seconds_map():
    table = json.loads(shard.DURATIONS.read_text())
    assert table and all(isinstance(k, str) and k.endswith(".py") for k in table)
    assert all(isinstance(v, (int, float)) and v >= 0 for v in table.values())


def test_durations_come_out_of_a_junit_file(tmp_path, monkeypatch):
    (tmp_path / "test" / "pkg").mkdir(parents=True)
    (tmp_path / "test" / "pkg" / "test_x.py").write_text("")
    (tmp_path / "test" / "test_y.py").write_text("")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="test.pkg.test_x" name="a" time="1.5"/>'
        '<testcase classname="test.pkg.test_x.TestK" name="b" time="2.0"/>'
        '<testcase classname="test.test_y" name="c" time="0.25"/>'
        '<testcase classname="test.gone.test_z" name="d" time="9"/>'
        "</testsuite></testsuites>"
    )
    monkeypatch.chdir(tmp_path)
    assert shard.durations_from_junit(junit) == {
        "test/pkg/test_x.py": 3.5,
        "test/test_y.py": 0.25,
    }


def test_main_prints_the_table(tmp_path, monkeypatch, capsys):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_y.py").write_text("")
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuites><testcase classname="test.test_y" name="c" time="0.5"/></testsuites>')
    monkeypatch.chdir(tmp_path)
    shard.main([str(junit)])
    assert json.loads(capsys.readouterr().out) == {"test/test_y.py": 0.5}
