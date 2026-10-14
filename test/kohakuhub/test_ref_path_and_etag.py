"""Pure logic of #58 B (``refs%2Fheads%2F<name>``) and #59 (``If-None-Match``); no database."""

from __future__ import annotations

import pytest

from kohakuhub.api.files import etag_matches
from kohakuhub.api.ref_path import FullRefMiddleware, short_ref_path

RESOLVE = b"/models/o/m/resolve/"
REVISION = b"/api/models/o/m/revision/"


@pytest.mark.parametrize(
    "raw, short",
    [
        (RESOLVE + b"refs%2Fheads%2Fdev/a/b.txt", RESOLVE + b"dev/a/b.txt"),
        (REVISION + b"refs%2fheads%2fdev", REVISION + b"dev"),
        (b"/api/datasets/o/d/tree/refs%2Fheads%2Fdev/sub%20dir", b"/api/datasets/o/d/tree/dev/sub%20dir"),
        (b"/api/spaces/o/s/tree/refs%2Fheads%2Fd%20ev", b"/api/spaces/o/s/tree/d%20ev"),
        (b"/o/m/resolve/refs%2Fheads%2Fdev/x", b"/o/m/resolve/dev/x"),  # the Hub's short form
        (RESOLVE + b"refs%2Fheads%2Ffeature%2Fx/a.txt", RESOLVE + b"feature%2Fx/a.txt"),  # a branch with a slash (#67)
        (REVISION + b"refs%2fheads%2ffeature%2fx%2fy", REVISION + b"feature%2fx%2fy"),
    ],
)
def test_a_full_ref_is_cut_to_the_branch(raw, short):
    assert short_ref_path(raw) == short


@pytest.mark.parametrize(
    "raw",
    [
        RESOLVE + b"dev/a.txt",  # nothing to do
        RESOLVE + b"refs/heads/dev/a.txt",  # literal slashes: split like the Hub does
        RESOLVE + b"refs%2Ftags%2Fv1/a.txt",
        RESOLVE + b"main/refs%2Fheads%2Fdev",  # a file path is not a revision
        b"/api/models/o/m/commits/refs%2Fheads%2Fdev",  # not a route the Hub reads this way
        b"/api/models/o/m/branch/refs%2Fheads%2Fdev",
        RESOLVE + b"refs%2Fheads%2F/a.txt",  # no name
    ],
)
def test_everything_else_is_left_alone(raw):
    assert short_ref_path(raw) is None


async def _run(scope):
    seen = []

    async def app(scope, receive, send):
        seen.append(scope)

    await FullRefMiddleware(app)(scope, None, None)
    return seen[0]


async def test_the_middleware_rewrites_path_and_raw_path_for_routing():
    scope = {"type": "http", "path": "/models/o/m/resolve/refs/heads/dev/a b.txt",
             "raw_path": RESOLVE + b"refs%2Fheads%2Fdev/a%20b.txt"}
    seen = await _run(scope)
    assert seen["path"] == "/models/o/m/resolve/dev/a b.txt"
    assert seen["raw_path"] == RESOLVE + b"dev/a%20b.txt"
    assert scope["path"].endswith("refs/heads/dev/a b.txt")  # the caller's scope is not touched


@pytest.mark.parametrize(
    "scope",
    [
        {"type": "lifespan"},
        {"type": "websocket", "path": "/x", "raw_path": RESOLVE + b"refs%2Fheads%2Fdev/a"},
        {"type": "http", "path": "/x"},  # a server that gives no raw_path
        {"type": "http", "path": "/x", "raw_path": RESOLVE + b"dev/a"},
    ],
)
async def test_the_middleware_passes_everything_else_through_unchanged(scope):
    assert await _run(scope) is scope


# ---------- #59 ----------


@pytest.mark.parametrize(
    "header, etag, expected",
    [
        ("abc", "abc", True),  # what the Hub's own 304 test sends, ours is unquoted
        ('"abc"', "abc", True),
        ('W/"abc"', "abc", True),
        ('"abc"', '"abc"', True),
        ('"x", "abc"', "abc", True),
        ('"x","abc"', "abc", True),
        ('"x" ,  W/"abc"', "abc", True),
        ("*", "abc", True),
        ('"abd"', "abc", False),
        ('"abc-extra"', "abc", False),
        ('"a,b", "abc"', "abc", True),  # a comma inside a tag does not split it
        ('"a,b"', "a", False),
        ("", "abc", False),
        (None, "abc", False),
        ("*", "", False),  # no ETag to match, nothing is "not modified"
        ('"abc"', "", False),
    ],
)
def test_if_none_match_uses_the_rfc_weak_comparison(header, etag, expected):
    assert etag_matches(header, etag) is expected
