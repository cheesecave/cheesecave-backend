"""A path with a NUL on a read is a 404 that reaches the client (#99).

The 404 names the path in ``X-Error-Message``; a NUL in a header value makes the HTTP server refuse to write the response and
drop the connection, so the client got nothing. The in-process test client does not check header bytes, so this runs against
the real server (uvicorn) of the ``live_server_url`` fixture.
"""

import httpx
import pytest

pytestmark = pytest.mark.hf_client

REPO = "owner/demo-model"


@pytest.mark.parametrize(
    "path",
    [
        f"/api/models/{REPO}/tree/main/a%00b",
        f"/{REPO}/resolve/main/a%00b",
        f"/api/models/{REPO}/tree/main/a%01b",
        f"/{REPO}/resolve/main/a%7fb",
        f"/api/models/{REPO}/revision/a%00b",
    ],
)
def test_a_control_character_in_a_read_path_is_answered(live_server_url, hf_api_token, path):
    response = httpx.get(live_server_url + path, headers={"Authorization": f"Bearer {hf_api_token}"}, trust_env=False)

    assert response.status_code == 404
    message = response.headers.get("x-error-message", "")
    assert all(ord(char) >= 0x20 and char != "\x7f" for char in message), repr(message)


def test_the_server_is_still_answering_after_one(live_server_url):
    httpx.get(live_server_url + f"/{REPO}/resolve/main/a%00b", trust_env=False)

    assert httpx.get(live_server_url + "/health", trust_env=False).status_code == 200


async def test_the_handler_cleans_the_message_header_whatever_its_case_and_touches_nothing_else():
    from starlette.exceptions import HTTPException
    from starlette.requests import Request

    from kohakuhub.main import _http_exception_handler

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    for headers, want in (
        (None, {}),
        ({}, {}),
        ({"X-Error-Message": "a\x00b"}, {"x-error-message": "a b"}),
        ({"x-error-message": "a\x1fb"}, {"x-error-message": "a b"}),
        ({"X-Error-Code": "RepoNotFound", "X-Error-Message": "é\x00"}, {"x-error-code": "RepoNotFound", "x-error-message": "é"}),
    ):
        response = await _http_exception_handler(request, HTTPException(404, detail="d", headers=headers))
        assert response.status_code == 404
        assert {k: v for k, v in response.headers.items() if k.startswith("x-")} == want
