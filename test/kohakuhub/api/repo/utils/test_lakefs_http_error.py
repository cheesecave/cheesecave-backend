"""`lakefs_error_to_http`: what a failed LakeFS call is answered with (#10).

The commit route wrapped every LakeFS failure in `HTTPException(500)`: a branch deleted while a commit was in flight was
a 500, and so was a path LakeFS refuses. A client reads the status: 404 `RevisionNotFound` is a wrong branch, 409 a
conflict, 400 a request to change, and only a fault of ours or of LakeFS is a 500.
"""

import httpx
import pytest
from fastapi import HTTPException

from kohakuhub.api.repo.utils.hf import HFErrorCode, lakefs_error_to_http
from test.kohakuhub.support.fakes import lakefs_error

CALL = dict(repo_id="owner/demo", revision="dev")


def _answer(error, what="Failed to upload a.txt", **kwargs):
    answered = lakefs_error_to_http(error, what, **{**CALL, **kwargs})
    assert isinstance(answered, HTTPException)
    return answered


def test_a_branch_lakefs_does_not_have_is_a_404_revision_not_found():
    answered = _answer(lakefs_error(404, "branch not found"))
    assert answered.status_code == 404
    assert answered.headers["X-Error-Code"] == HFErrorCode.REVISION_NOT_FOUND
    assert answered.detail == {"error": "Revision 'dev' not found in repository 'owner/demo'"}
    assert answered.headers["X-Error-Message"] == answered.detail["error"]


def test_a_path_lakefs_does_not_have_is_a_404_entry_not_found_when_the_call_asked_for_a_path():
    answered = _answer(lakefs_error(404, "not found"), missing="entry", path="docs/a.txt")
    assert answered.status_code == 404
    assert answered.headers["X-Error-Code"] == HFErrorCode.ENTRY_NOT_FOUND
    assert answered.detail == {"error": "Entry 'docs/a.txt' not found in repository 'owner/demo' at revision 'dev'"}


@pytest.mark.parametrize("status", [400, 409])
def test_a_request_lakefs_refuses_keeps_its_status_and_says_what_lakefs_said(status):
    answered = _answer(lakefs_error(status, "path in query has an error: empty value is not allowed"))
    assert answered.status_code == status
    assert answered.headers["X-Error-Code"] == HFErrorCode.BAD_REQUEST
    assert answered.detail == {"error": "Failed to upload a.txt: path in query has an error: empty value is not allowed"}
    assert answered.headers["X-Error-Message"] == answered.detail["error"]


@pytest.mark.parametrize("status", [401, 403, 410, 500, 502, 503])
def test_every_other_answer_of_lakefs_is_a_server_error(status):
    """401 and 403 are our credentials, not the client's request: the client cannot change them."""
    answered = _answer(lakefs_error(status, "boom"))
    assert answered.status_code == 500
    assert answered.headers["X-Error-Code"] == HFErrorCode.SERVER_ERROR
    assert "Failed to upload a.txt" in answered.detail["error"] and "boom" in answered.detail["error"]


@pytest.mark.parametrize("error", [httpx.ConnectError("connection refused"), httpx.ReadTimeout("slow"), RuntimeError("anything")])
def test_a_failure_that_is_no_answer_of_lakefs_is_a_server_error(error):
    answered = _answer(error)
    assert answered.status_code == 500 and answered.headers["X-Error-Code"] == HFErrorCode.SERVER_ERROR
    assert str(error) in answered.detail["error"]


def test_the_message_is_what_lakefs_said_or_else_the_error():
    assert "empty value" in _answer(lakefs_error(400, "empty value")).detail["error"]
    bare = _answer(lakefs_error(409))  # an answer without a body: the error itself is the message
    assert "409" in bare.detail["error"]


def test_the_header_is_one_line_whatever_the_message_holds():
    answered = _answer(lakefs_error(400, "first line\nsecond line\r\nthird"))
    assert "\n" not in answered.headers["X-Error-Message"] and "\r" not in answered.headers["X-Error-Message"]
    assert "second line" in answered.headers["X-Error-Message"]
