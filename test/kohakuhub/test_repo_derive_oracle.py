"""The derivation rules against what the real Hub answered for 542 real repositories.

``test/fixtures/real_hub_derive.json`` holds, per repository, the file names, the few small JSON
files the rules read (cut to the keys they use) and the Hub's own answer. Nothing is fetched.

Criteria, in order of importance:
  * never wrong: a value we return is the Hub's (the Hub itself answers null for repositories it
    has not analysed yet, which is not an error on our side);
  * not silent: enough values are still returned, so a rule that stops firing is noticed.
"""

import json
from pathlib import Path

import pytest

from kohakuhub.repo_derive import derive

ROWS = json.loads((Path(__file__).parents[1] / "fixtures/real_hub_derive.json").read_text())


@pytest.fixture(scope="module")
def results():
    return [(row, derive(row["card"], row["names"], row["files"])) for row in ROWS]


def test_the_sample_is_big_enough_to_mean_something():
    assert len(ROWS) >= 500


def test_a_library_we_name_is_the_hubs(results):
    wrong = [
        (r["id"], d["library_name"], r["real"]["library_name"])
        for r, d in results
        if d["library_name"] and r["real"]["library_name"] and d["library_name"] != r["real"]["library_name"]
    ]
    assert wrong == []


def test_a_task_we_name_is_the_hubs(results):
    wrong = [
        (r["id"], d["pipeline_tag"], r["real"]["pipeline_tag"])
        for r, d in results
        if d["pipeline_tag"] and r["real"]["pipeline_tag"] and d["pipeline_tag"] != r["real"]["pipeline_tag"]
    ]
    assert wrong == []


def test_every_tag_we_add_is_one_the_hub_lists(results):
    """Checked where the Hub analysed the repository (it names a library): a fresh one comes back
    without the derived tags at all."""
    extra = [
        (r["id"], [t for t in d["tags"] if t not in r["real"]["tags"]])
        for r, d in results
        if r["real"]["library_name"] is not None and any(t not in r["real"]["tags"] for t in d["tags"])
    ]
    assert extra == []


def test_a_config_value_we_copy_is_the_hubs(results):
    wrong = [
        (r["id"], k)
        for r, d in results
        if d["config"] and r["real"]["config"]
        for k, v in d["config"].items()
        if k in r["real"]["config"] and r["real"]["config"][k] != v
    ]
    assert wrong == []


def test_the_rules_still_find_most_of_what_the_hub_knows(results):
    """Floors a little under what was measured (library 351 of 441 holdout + 109 of 116), so that
    a regression to silence fails and normal drift does not."""
    named = [(r, d) for r, d in results if r["real"]["library_name"]]
    right = sum(d["library_name"] == r["real"]["library_name"] for r, d in named)
    assert right / len(named) >= 0.80

    tasks = [(r, d) for r, d in results if r["real"]["pipeline_tag"]]
    assert sum(d["pipeline_tag"] == r["real"]["pipeline_tag"] for r, d in tasks) >= 100

    configs = [(r, d) for r, d in results if r["real"]["config"] and d["config"]]
    equal = sum(
        all(r["real"]["config"].get(k) == v for k, v in d["config"].items()) for r, d in configs
    )
    assert equal / len(configs) >= 0.95


# ---------- the safetensors summary against the real Hub (#63, part 3) ----------

from kohakuhub import repo_derive  # noqa: E402

ST_ROWS = json.loads((Path(__file__).parents[1] / "fixtures/real_hub_safetensors.json").read_text())


def _headers(row):
    """The fixture keeps each file's tensors as ``[dtype, shape, how many]``: expand them again."""
    return {
        name: {f"t{i}.{j}": {"dtype": d, "shape": shape} for i, (d, shape, count) in enumerate(groups) for j in range(count)}
        for name, groups in row["headers"].items()
    }


def _summary(row):
    files = repo_derive.weight_files(row["names"])
    return None if files is None else repo_derive.safetensors_summary(files, _headers(row), row["quantized"])


def test_the_safetensors_sample_is_big_enough_to_mean_something():
    assert len(ST_ROWS) >= 150


def test_a_safetensors_summary_we_give_is_the_hubs_exactly():
    """172 real repositories (files read from huggingface.co): every summary we give equals the Hub's,
    parameters per dtype and total. What we leave null is not an error."""
    wrong = [(r["id"], _summary(r), r["real"]) for r in ST_ROWS if _summary(r) is not None and _summary(r) != r["real"]]

    assert wrong == []


def test_the_safetensors_rules_still_answer_most_of_what_they_can():
    """Measured: 147 of 172 are answered (the rest are quantized, hold two sets of weights or are not
    complete). A floor, so that a rule that stops firing is noticed."""
    answered = sum(_summary(r) is not None for r in ST_ROWS)

    assert answered >= 140


def test_a_quantized_model_is_never_summed_whatever_its_files_say():
    quantized = [r for r in ST_ROWS if r["quantized"]]

    assert quantized and all(_summary(r) is None for r in quantized)
