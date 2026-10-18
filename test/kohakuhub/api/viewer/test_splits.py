"""L0 of the data explorer: which files make which split of which config.

The cases are the naming rules Hugging Face documents for dataset repositories (file names, directory names,
keyword equivalences, ``<split>-00000-of-00003`` shards) and the ``configs`` block of the README front matter.
"""

import pytest

from kohakuhub.api.viewer import splits


def files(*paths, size=1):
    return [{"path": p, "size": size} for p in paths]


def shape(result):
    return {c["config"]: {s["split"]: [f["path"] for f in s["files"]] for s in c["splits"]} for c in result["configs"]}


def test_a_single_file_of_any_name_is_the_train_split():
    r = splits.resolve(files("data.csv", "README.md"))
    assert r["source"] == "naming"
    assert shape(r) == {"default": {"train": ["data.csv"]}}


@pytest.mark.parametrize(
    "name,split",
    [
        ("train.csv", "train"), ("my_train_file.csv", "train"), ("train1.csv", "train"), ("training.csv", "train"),
        ("validation.csv", "validation"), ("valid.csv", "validation"), ("val-00.csv", "validation"), ("dev.csv", "validation"),
        ("test.csv", "test"), ("eval.csv", "test"), ("my_testing_file.csv", "test"), ("evaluation.csv", "test"),
    ],
)
def test_file_name_keywords_and_their_equivalents(name, split):
    assert splits.split_of(name) == split


@pytest.mark.parametrize("name", ["testfile.csv", "traindata.csv", "contest.csv", "latest.csv", "data.csv"])
def test_a_keyword_glued_to_letters_is_not_a_split(name):
    assert splits.split_of(name) is None


def test_directory_names_make_splits():
    r = splits.resolve(files("data/train/a.csv", "data/train/b.csv", "data/test/c.csv", "data/validation/d.csv"))
    assert shape(r) == {"default": {"train": ["data/train/a.csv", "data/train/b.csv"], "test": ["data/test/c.csv"], "validation": ["data/validation/d.csv"]}}


def test_custom_split_names_come_from_the_shard_pattern():
    r = splits.resolve(files("data/train-00000-of-00002.parquet", "data/train-00001-of-00002.parquet", "data/random-00000-of-00001.parquet"))
    assert shape(r) == {"default": {"train": ["data/train-00000-of-00002.parquet", "data/train-00001-of-00002.parquet"], "random": ["data/random-00000-of-00001.parquet"]}}


def test_a_keyword_shard_name_maps_to_its_canonical_split():
    assert splits.split_of("data/eval-00000-of-00001.parquet") == "test"
    assert splits.split_of("data/dev-00000-of-00003.parquet") == "validation"


def test_readme_hidden_and_non_data_files_are_ignored():
    r = splits.resolve(files("README.md", ".gitattributes", "LICENSE", "script.py", "train.csv"))
    assert shape(r) == {"default": {"train": ["train.csv"]}}


def test_no_data_files_means_no_config():
    assert splits.resolve(files("README.md", "notes.txt.bak"))["configs"] == []


YAML = """---
configs:
- config_name: small
  data_files:
  - split: train
    path: data/train-*.parquet
  - split: test
    path: ["data/test-0.parquet", "extra/*.parquet"]
- config_name: other
  default: true
  data_dir: more
  data_files: "*.csv"
---
# card
"""


def test_the_readme_configs_block_wins_over_file_names():
    paths = files("data/train-0.parquet", "data/train-1.parquet", "data/test-0.parquet", "extra/e.parquet", "more/x.csv", "more/sub/y.csv", "README.md")
    r = splits.resolve(paths, YAML)
    assert r["source"] == "yaml"
    assert shape(r) == {
        "small": {"train": ["data/train-0.parquet", "data/train-1.parquet"], "test": ["data/test-0.parquet", "extra/e.parquet"]},
        "other": {"train": ["more/x.csv"]},
    }
    assert [c["default"] for c in r["configs"]] == [False, True]


def test_a_double_star_crosses_directories_and_a_single_star_does_not():
    yml = "---\nconfigs:\n- config_name: c\n  data_files:\n  - split: train\n    path: 'a/**/*.csv'\n---\n"
    r = splits.resolve(files("a/x.csv", "a/b/y.csv", "a/b/c/z.csv", "b/w.csv"), yml)
    assert shape(r) == {"c": {"train": ["a/b/c/z.csv", "a/b/y.csv", "a/x.csv"]}}
    yml = "---\nconfigs:\n- config_name: c\n  data_files:\n  - split: train\n    path: 'a/*.csv'\n---\n"
    assert shape(splits.resolve(files("a/x.csv", "a/b/y.csv"), yml)) == {"c": {"train": ["a/x.csv"]}}


def test_a_config_without_a_split_is_train_and_front_matter_that_does_not_parse_is_ignored():
    assert splits.front_matter("---\n: : :\n  - [\n---\n") == {}
    assert splits.front_matter("no front matter") == {}
    assert splits.front_matter(None) == {}
    assert splits.front_matter("---\n- a list\n---\n") == {}
    r = splits.resolve(files("a.csv"), "---\nconfigs:\n- config_name: only\n  data_files:\n  - path: a.csv\n---\n")
    assert shape(r) == {"only": {"train": ["a.csv"]}}


def test_mixed_formats_in_a_split_are_reported_not_hidden():
    r = splits.resolve(files("train.csv", "train.parquet"))
    split = r["configs"][0]["splits"][0]
    assert split["format"] == "csv" and [f["path"] for f in split["files"]] == ["train.csv"]
    assert split["notes"] and "mixed formats" in split["notes"][0]


def test_formats():
    assert [splits.format_of(p) for p in ("a.parquet", "a.CSV", "a.tsv", "a.jsonl", "a.json", "a.tar", "a.arrow", "a.txt", "a.bin", "README.md", ".hidden.csv")] == [
        "parquet", "csv", "csv", "jsonl", "json", "webdataset", "arrow", "text", None, None, None]
