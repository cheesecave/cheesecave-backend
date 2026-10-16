"""The HTTP answer of a refused history operation (#103): a branch that is not there is `RevisionNotFound`, the rest keeps its own."""

from kohakuhub.api.commit.records import OperationRefused
from kohakuhub.api.repo.utils.hf import revision_not_found_http


def test_a_refusal_that_is_a_branch_not_there_is_revision_not_found_with_its_own_words():
    refused = OperationRefused(404, {"error": "Branch not found: dev"})

    answer = refused.to_http()

    assert answer.status_code == 404
    assert answer.detail == {"error": "Branch not found: dev"}
    assert answer.headers["X-Error-Code"] == "RevisionNotFound"
    assert answer.headers["X-Error-Message"] == "Branch not found: dev"


def test_the_other_refusals_keep_their_status_and_their_detail_and_say_no_code():
    detail = {"error": "conflicts with a later change", "conflicts": ["a.txt", "b.txt"]}

    answer = OperationRefused(409, detail, rounds=[("commit", {})]).to_http()

    assert answer.status_code == 409
    assert answer.detail == detail  # the list of conflicts is part of the answer
    assert not answer.headers or "X-Error-Code" not in answer.headers


def test_a_404_with_a_message_the_header_cannot_hold_still_has_a_header():
    answer = revision_not_found_http("Branch not found: 模型\nsecond line")

    assert answer.status_code == 404
    assert answer.detail == {"error": "Branch not found: 模型\nsecond line"}  # the body keeps what was said
    assert answer.headers["X-Error-Code"] == "RevisionNotFound"
    answer.headers["X-Error-Message"].encode("latin-1")  # what the server does with it
    assert "\n" not in answer.headers["X-Error-Message"]
