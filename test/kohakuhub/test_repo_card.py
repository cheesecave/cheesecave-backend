"""A repository card: README front matter -> the Hub's ``cardData`` and the fields it declares."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from kohakuhub import repo_card


def card(text: str) -> bytes:
    return ("---\n" + text + "\n---\n# Title\n").encode()


# --- reading the front matter ------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"# no front matter",
        b"",
        b"---\ntags: [unterminated",
        b"---\ntags: [a]\n",  # never closed
        b"---\n- a\n- b\n---\n",  # a list, not a mapping
        b"---\nx: &a [1]\ny: *a\n---\n",  # aliases are refused
        b"---\nbad: : yaml\n---\n",
        b"---\n---\n",
    ],
)
def test_content_without_a_usable_mapping_has_no_card(content):
    assert repo_card.load_front_matter(content) is None


def test_a_byte_order_mark_windows_line_ends_and_the_dots_closer_are_accepted():
    for source in (
        b"\xef\xbb\xbf---\nlicense: mit\n---\n",
        b"---\r\nlicense: mit\r\n---\r\n",
        b"---\nlicense: mit\n...\n",
    ):
        assert repo_card.load_front_matter(source) == {"license": "mit"}


def test_blank_lines_before_the_opening_marker_are_skipped_like_on_the_hub():
    assert repo_card.load_front_matter(b"\n\n  \n---\nlicense: mit\n---\n") == {"license": "mit"}
    assert repo_card.load_front_matter(b"\nhello\n---\nlicense: mit\n---\n") is None


def test_yes_no_and_on_off_stay_strings_like_on_the_hub_but_true_false_are_booleans():
    loaded = repo_card.load_front_matter(card("a: yes\nb: no\nc: on\nd: off\ne: true\nf: False"))

    assert loaded == {"a": "yes", "b": "no", "c": "on", "d": "off", "e": True, "f": False}


def test_a_large_declaration_is_read_but_an_unbounded_one_is_not():
    big = "\n".join(f"- {{name: f{i}, dtype: string}}" for i in range(3000))
    assert len(repo_card.load_front_matter(card("dataset_info:\n" + "\n".join("  " + l for l in big.splitlines()))) ["dataset_info"]) == 3000
    deep = "a: " + "[" * 40 + "]" * 40
    assert repo_card.load_front_matter(card(deep)) is None
    wide = "a: [" + ",".join(["1"] * (repo_card.MAX_NODES + 10)) + "]"
    assert repo_card.load_front_matter(card(wide)) is None


# --- cardData as the Hub returns it ---------------------------------------


def test_the_declared_values_come_back_as_written():
    raw = repo_card.load_front_matter(card("language: en\nlicense: mit\ntags:\n- exbert\nmodel-index:\n- name: x\n  results: []"))

    assert repo_card.clean_card(raw) == {"language": "en", "license": "mit", "tags": ["exbert"], "model-index": [{"name": "x", "results": []}]}


def test_values_json_cannot_carry_are_made_safe():
    raw = {"created": date(2020, 1, 2), "at": datetime(2020, 1, 2, 3, 4, 5), "n": float("nan"), "inf": float("inf"), "blob": b"x", "set": {"a"}, "nested": {"d": date(2021, 1, 1)}}

    cleaned = repo_card.clean_card(raw)

    assert cleaned["created"] == "2020-01-02" and cleaned["at"] == "2020-01-02T03:04:05"
    assert cleaned["n"] is None and cleaned["inf"] is None and "blob" not in cleaned
    assert cleaned["set"] == ["a"] and cleaned["nested"] == {"d": "2021-01-01"}
    json.dumps(cleaned, allow_nan=False)


def test_keys_are_strings_and_the_key_hf_hub_treats_as_an_option_is_dropped():
    raw = {1: "one", 2.5: "float", None: "none", "ignore_metadata_errors": True, "ok": {2: "two"}}

    cleaned = repo_card.clean_card(raw)

    assert cleaned == {"1": "one", "2.5": "float", "None": "none", "ok": {"2": "two"}}


@pytest.mark.parametrize(
    "tags,expected",
    [("exbert", ["exbert"]), (["a", 1, None, "b", ["x"]], ["a", "1", "b"]), (None, None), (5, None), ({}, None)],
)
def test_tags_are_always_a_list_of_strings_or_absent(tags, expected):
    cleaned = repo_card.clean_card({"tags": tags, "license": "mit"})

    assert cleaned.get("tags") == expected and cleaned["license"] == "mit"


def test_every_evaluation_metric_is_unverified_whatever_the_readme_says():
    raw = {"model-index": [{"name": "m", "results": [{"metrics": [{"type": "f1", "value": 1, "verified": True}, {"type": "acc", "value": 2}]}, {"task": {}}]}]}

    cleaned = repo_card.clean_card(raw)

    metrics = cleaned["model-index"][0]["results"][0]["metrics"]
    assert metrics == [{"type": "f1", "value": 1, "verified": False}, {"type": "acc", "value": 2, "verified": False}]


@pytest.mark.parametrize("index", ["text", 5, [1, "x"], [{"results": "x"}], [{"results": [None, {"metrics": "x"}, {"metrics": [3]}]}]])
def test_a_malformed_model_index_is_left_alone(index):
    assert repo_card.clean_card({"model-index": index})["model-index"] == index


def test_an_empty_card_is_no_card():
    assert repo_card.clean_card({}) is None
    assert repo_card.clean_card(None) is None
    assert repo_card.clean_card({"ignore_metadata_errors": True}) is None


def test_a_card_over_the_size_limit_drops_its_biggest_values_first():
    raw = {"license": "mit", "huge": ["x" * 1000] * 2000, "big": ["y" * 100] * 100}

    cleaned = repo_card.clean_card(raw)

    assert cleaned == {"license": "mit", "big": ["y" * 100] * 100}
    assert len(json.dumps(cleaned)) <= repo_card.MAX_CARD_JSON


def test_a_very_deep_value_is_cut_instead_of_overflowing():
    value = leaf = []
    for _ in range(200):
        leaf.append([])
        leaf = leaf[0]

    cleaned = repo_card.clean_card({"deep": value, "k": 1})

    assert cleaned["k"] == 1 and "deep" in cleaned
    json.dumps(cleaned)


# --- what the card declares --------------------------------------------------


def test_declared_pipeline_library_and_sdk_must_be_strings():
    assert repo_card.declared({"pipeline_tag": "fill-mask", "library_name": "transformers", "sdk": "gradio"}) == {
        "pipeline_tag": "fill-mask",
        "library_name": "transformers",
        "sdk": "gradio",
    }
    assert repo_card.declared({"pipeline_tag": ["x"], "library_name": 3, "sdk": ""}) == {
        "pipeline_tag": None,
        "library_name": None,
        "sdk": None,
    }
    assert repo_card.declared(None) == {"pipeline_tag": None, "library_name": None, "sdk": None}


def test_model_tags_follow_the_hubs_order():
    c = {
        "library_name": "transformers",
        "pipeline_tag": "text-generation",
        "tags": ["exbert", "transformers"],
        "language": ["en", "fr"],
        "datasets": ["bookcorpus"],
        "base_model": ["org/base"],
        "license": "MIT",
    }

    assert repo_card.compose_tags(c, "model") == [
        "transformers", "text-generation", "exbert", "en", "fr", "dataset:bookcorpus", "base_model:org/base", "license:mit", "region:us",
    ]


def test_a_base_model_relation_adds_the_relation_tag():
    assert repo_card.compose_tags({"base_model": "org/base", "base_model_relation": "quantized"}, "model") == [
        "base_model:org/base", "base_model:quantized:org/base", "region:us",
    ]


def test_model_tags_of_an_empty_card_are_the_region_alone():
    assert repo_card.compose_tags(None, "model") == ["region:us"]
    assert repo_card.compose_tags({}, "space") == ["region:us"]


def test_dataset_tags_prefix_the_declared_groups_except_the_size_which_the_hub_measures():
    c = {
        "task_categories": ["text-classification"],
        "task_ids": "sentiment-classification",
        "annotations_creators": ["expert-generated"],
        "language_creators": ["found"],
        "multilinguality": ["monolingual"],
        "source_datasets": ["original"],
        "language": ["en"],
        "license": "other",
        "size_categories": ["100K<n<1M"],
        "tags": ["qa-nli", "format:parquet"],
    }

    assert repo_card.compose_tags(c, "dataset") == [
        "task_categories:text-classification", "task_ids:sentiment-classification", "annotations_creators:expert-generated",
        "language_creators:found", "multilinguality:monolingual", "source_datasets:original", "language:en", "license:other",
        "format:parquet", "region:us", "qa-nli",
    ]


def test_space_tags_start_with_the_sdk():
    assert repo_card.compose_tags({"sdk": "gradio", "tags": ["leaderboard"]}, "space") == ["gradio", "leaderboard", "region:us"]


def test_tag_values_that_are_not_text_are_ignored_and_duplicates_collapse():
    c = repo_card.clean_card({"tags": ["a", "a", 3, None], "language": [None, "en", "en"], "license": ["mit"], "datasets": 7})

    assert repo_card.compose_tags(c, "model") == ["a", "3", "en", "license:mit", "region:us"]  # a license list is read too


# --- validating a README the way ``/api/validate-yaml`` answers ----------------


def test_a_readme_with_a_good_card_has_nothing_to_say():
    assert repo_card.check_readme("---\nlicense: mit\n---\n# Title") == ([], [])


@pytest.mark.parametrize("content", ["# just markdown", "---\n---\n# empty", "---\nlicense: mit\n# never closed", ""])
def test_a_readme_without_usable_metadata_only_gets_the_hubs_warning(content):
    errors, warnings = repo_card.check_readme(content)

    assert errors == [] and warnings == [{"message": "empty or missing yaml metadata in repo card", "help": repo_card.CARD_HELP}]


def test_metadata_that_is_not_a_mapping_is_an_error():
    errors, warnings = repo_card.check_readme("---\n- a\n- b\n---\n")

    assert errors == [{"message": '"value" must be of type object', "path": []}] and warnings == []


def test_invalid_yaml_is_an_error_that_says_where():
    errors, warnings = repo_card.check_readme("---\nlicense: [unclosed\n  bad: : yaml\n---\n")

    assert len(errors) == 1 and errors[0]["message"].startswith("Invalid YAML in README.md: ") and errors[0]["path"] == []
    assert warnings == []


def test_a_card_over_the_limits_is_an_error_not_a_crash():
    errors, _ = repo_card.check_readme("---\na: " + "[" * 40 + "]" * 40 + "\n---\n")

    assert errors and errors[0]["message"].startswith("Invalid YAML in README.md: ")


# --- the real Hub as the oracle -------------------------------------------------
# test/fixtures/real_hub_cards.json holds the front matter of public repositories (models,
# datasets, spaces) next to what huggingface.co answered for them on 2026-10-14.

import pathlib

REAL = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures/real_hub_cards.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", REAL, ids=[c["repo"] for c in REAL])
def test_the_card_is_what_the_hub_answers_as_card_data(case):
    cleaned = repo_card.clean_card(repo_card.load_front_matter(("---\n" + case["front_matter"] + "\n---\n").encode()))
    expected = json.loads(json.dumps(case["cardData"]))
    # The Hub's own evaluation service verifies some results (``verified: true``); no README can say so
    for entry in expected["model-index"] if expected and isinstance(expected.get("model-index"), list) else []:
        for result in entry.get("results", []):
            for metric in result.get("metrics", []):
                metric["verified"] = False

    assert cleaned == expected


@pytest.mark.parametrize("case", REAL, ids=[c["repo"] for c in REAL])
def test_the_declared_fields_agree_with_the_hub(case):
    cleaned = repo_card.clean_card(repo_card.load_front_matter(("---\n" + case["front_matter"] + "\n---\n").encode()))
    declared = repo_card.declared(cleaned)
    tags = repo_card.compose_tags(cleaned, case["type"])

    if declared["pipeline_tag"]:
        assert declared["pipeline_tag"] == case["pipeline_tag"]
    if declared["library_name"]:
        assert declared["library_name"] == case["library_name"]
    assert [t for t in tags if t not in case["tags"] and t != "region:us"] == []  # nothing declared that the Hub does not say
