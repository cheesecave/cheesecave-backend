"""A LakeFS error is read by its status, not by the words in its message (#10).

The text of an error holds URLs, repository names and ids: `404` or "not found" is found in it by chance
(`.../repositories/m-owner-model404-abc/...`, a branch named `conflict`), and so is "ref" in every URL with `/refs/`.
`LakeFSRestClient` raises `httpx.HTTPStatusError`, which holds the response.
"""

import httpx
import pytest

from kohakuhub.utils.lakefs import is_lakefs_conflict_error, is_lakefs_not_found_error, lakefs_status
from test.kohakuhub.support.fakes import lakefs_error


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 500, 503])
def test_the_status_of_a_lakefs_answer_is_read_from_the_response(status):
    assert lakefs_status(lakefs_error(status, "whatever")) == status


def test_a_failure_that_is_no_answer_of_lakefs_has_no_status():
    assert lakefs_status(RuntimeError("404 not found")) is None
    assert lakefs_status(httpx.ConnectError("connection refused")) is None
    assert lakefs_status(ValueError("Revision 'x' not found as branch or commit")) is None


def test_not_found_is_a_404_and_nothing_else():
    assert is_lakefs_not_found_error(lakefs_error(404, "branch not found")) is True
    assert is_lakefs_not_found_error(lakefs_error(404)) is True  # whatever the body says: LakeFS says "not found" for a ref and for an object alike
    for other in (lakefs_error(409, "not found"), lakefs_error(500, "not found"), lakefs_error(400, "404")):
        assert is_lakefs_not_found_error(other) is False
    # the words are in the text, no status: a message is not a status
    assert is_lakefs_not_found_error(RuntimeError("404 not found")) is False
    assert is_lakefs_not_found_error(httpx.ConnectError("not found")) is False


def test_a_conflict_is_a_409_and_nothing_else():
    assert is_lakefs_conflict_error(lakefs_error(409, "branch already exists: not unique")) is True
    assert is_lakefs_conflict_error(lakefs_error(409)) is True
    for other in (lakefs_error(404, "conflict"), lakefs_error(500, "conflict"), lakefs_error(400, "409")):
        assert is_lakefs_conflict_error(other) is False
    assert is_lakefs_conflict_error(RuntimeError("409 conflict")) is False


def test_a_name_with_the_digits_in_it_is_not_an_answer():
    """The URL in the message of a 500 names a repository `...404...` and a branch `conflict`."""
    error = lakefs_error(500, "boom", path="/repositories/m-owner-model404-abc/branches/conflict")
    assert "404" in str(error) and "conflict" in str(error)
    assert is_lakefs_not_found_error(error) is False
    assert is_lakefs_conflict_error(error) is False
