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
