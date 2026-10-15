"""The fields of a repository info response, per type, as the Hub shapes them (#62)."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from kohakuhub.api.repo.utils import hf

SHA = "a" * 40
ROW = SimpleNamespace(id=7, namespace="owner", name="repo", created_at=datetime(2025, 1, 2, 3, 4, 5), private=False, downloads=3, likes_count=2, used_bytes=10)
CARD = {
    "card": {"license": "mit", "pipeline_tag": "fill-mask", "model-index": [{"name": "x"}], "paperswithcode_id": "squad"},
    "derived": {"tags": ["fill-mask", "license:mit", "region:us"], "pipeline_tag": "fill-mask", "library_name": "transformers", "sdk": "gradio"},
}


def fields(repo_type, card=None):
    return hf.repo_info_fields(ROW, repo_type, SHA, "2025-01-02T00:00:00.000Z", storage=False, card=card)


def test_ids_are_strings_like_the_hubs():
    for repo_type in ("model", "dataset", "space"):
        assert fields(repo_type)["_id"] == "7" and fields(repo_type)["id"] == "owner/repo"


def test_a_model_without_a_card_has_empty_declared_fields_and_no_card_keys():
    body = fields("model")

    assert body["tags"] == [] and body["pipeline_tag"] is None and body["library_name"] is None
    assert "cardData" not in body and "model-index" not in body
    assert body["modelId"] == "owner/repo" and body["spaces"] == [] and body["downloads"] == 3
    assert body["sha"] == SHA  # whatever the caller passes is the sha: it is not ours to shorten


def test_a_model_with_a_card_answers_what_the_card_declares():
    body = fields("model", CARD)

    assert body["tags"] == ["fill-mask", "license:mit", "region:us"]
    assert body["pipeline_tag"] == "fill-mask" and body["library_name"] == "transformers"
    assert body["cardData"] == CARD["card"] and body["model-index"] == [{"name": "x"}]


def test_a_card_without_an_evaluation_index_says_none_for_it():
    body = fields("model", {"card": {"license": "mit"}, "derived": CARD["derived"]})

    assert body["cardData"] == {"license": "mit"} and body["model-index"] is None


def test_a_dataset_has_no_model_fields():
    body = fields("dataset", CARD)

    for absent in ("modelId", "pipeline_tag", "library_name", "spaces", "models", "datasets", "sdk"):
        assert absent not in body
    assert body["cardData"] == CARD["card"] and body["paperswithcode_id"] == "squad" and body["downloads"] == 3
    assert body["tags"] == CARD["derived"]["tags"]


def test_a_dataset_paperswithcode_id_is_only_a_declared_string():
    assert "paperswithcode_id" not in fields("dataset")
    assert "paperswithcode_id" not in fields("dataset", {"card": {"paperswithcode_id": 5}, "derived": CARD["derived"]})


def test_a_space_has_its_sdk_and_no_download_counts():
    body = fields("space", CARD)

    assert body["sdk"] == "gradio" and body["cardData"] == CARD["card"]
    for absent in ("modelId", "pipeline_tag", "library_name", "downloads", "models", "datasets", "spaces"):
        assert absent not in body
    assert fields("space")["sdk"] is None


def test_the_fields_stay_what_the_other_info_routes_add_to():
    body = fields("model")

    for kept in ("author", "sha", "lastModified", "createdAt", "private", "disabled", "gated", "likes", "usedStorage"):
        assert kept in body
    assert body["createdAt"].endswith("Z")


def test_expand_answers_only_what_was_asked_with_null_for_what_there_is_not():
    response = hf.hf_repo_info_response(fields("model", CARD), ["cardData", "tags", "safetensors"], siblings=None)

    assert json.loads(response.body) == {
        "_id": "7", "id": "owner/repo", "cardData": CARD["card"], "tags": CARD["derived"]["tags"], "safetensors": None,
    }


CONFIG = {"model_type": "bert", "architectures": ["BertForMaskedLM"]}


def test_a_model_answers_the_config_its_files_gave():
    assert fields("model", {**CARD, "info": {"config": CONFIG}})["config"] == CONFIG


@pytest.mark.parametrize("info", [None, {}, {"config": {}}])
def test_a_model_without_a_config_has_no_config_key(info):
    assert "config" not in fields("model", {**CARD, "info": info})
    assert "config" not in fields("model", CARD)  # a card that has no info at all


@pytest.mark.parametrize("repo_type", ["dataset", "space"])
def test_only_models_have_a_config(repo_type):
    assert "config" not in fields(repo_type, {**CARD, "info": {"config": CONFIG}})


def test_expand_config_is_answered_or_null():
    asked = hf.hf_repo_info_response(fields("model", {**CARD, "info": {"config": CONFIG}}), ["config"], siblings=None)
    unknown = hf.hf_repo_info_response(fields("model", CARD), ["config"], siblings=None)

    assert json.loads(asked.body)["config"] == CONFIG
    assert json.loads(unknown.body)["config"] is None


def test_config_is_a_property_that_needs_the_card():
    assert "config" in hf.CARD_PROPERTIES


SUMMARY = {"parameters": {"BF16": 20, "F32": 3}, "total": 23}


def test_a_model_answers_the_summary_of_its_weights():
    assert fields("model", {**CARD, "info": {"safetensors": SUMMARY}})["safetensors"] == SUMMARY


@pytest.mark.parametrize("info", [None, {}, {"safetensors": None}, {"config": CONFIG}])
def test_a_model_without_a_summary_has_no_safetensors_key(info):
    """Not yet computed, or not one to give: the Hub's ``None`` is the missing key (``expand`` says null)."""
    assert "safetensors" not in fields("model", {**CARD, "info": info})


@pytest.mark.parametrize("repo_type", ["dataset", "space"])
def test_only_models_have_a_safetensors_summary(repo_type):
    assert "safetensors" not in fields(repo_type, {**CARD, "info": {"safetensors": SUMMARY}})


def test_the_config_and_the_summary_come_together():
    out = fields("model", {**CARD, "info": {"config": CONFIG, "safetensors": SUMMARY}})

    assert out["config"] == CONFIG and out["safetensors"] == SUMMARY


def test_expand_safetensors_is_answered_or_null():
    asked = hf.hf_repo_info_response(fields("model", {**CARD, "info": {"safetensors": SUMMARY}}), ["safetensors"], siblings=None)
    unknown = hf.hf_repo_info_response(fields("model", CARD), ["safetensors"], siblings=None)

    assert json.loads(asked.body)["safetensors"] == SUMMARY
    assert json.loads(unknown.body)["safetensors"] is None


def test_safetensors_is_a_property_that_needs_the_card():
    assert "safetensors" in hf.CARD_PROPERTIES


@pytest.mark.parametrize("name", ["cardData", "model-index", "pipeline_tag", "library_name"])
def test_the_card_properties_are_valid_expand_values_for_models(name):
    assert hf.expand_error("model", [name]) is None


# --- expand[] on a request that is not a Starlette one ---------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"expand[]": "tags"}, ["tags"]),
        ({"expand[]": ["tags", "likes"]}, ["tags", "likes"]),
        ({}, []),
        ({"expand[]": None}, []),
    ],
)
def test_the_bracket_spelling_is_read_from_a_plain_mapping_too(query, expected):
    from kohakuhub.api.repo.routers import info

    assert info._bracket_expand(SimpleNamespace(query_params=query)) == expected


def test_a_request_without_a_query_has_no_bracket_values():
    from kohakuhub.api.repo.routers import info

    assert info._bracket_expand(SimpleNamespace()) == []


def test_the_bracket_spelling_is_read_from_starlette_query_params():
    from starlette.datastructures import QueryParams

    from kohakuhub.api.repo.routers import info

    request = SimpleNamespace(query_params=QueryParams("expand[]=a&expand[]=b&x=1"))

    assert info._bracket_expand(request) == ["a", "b"]
