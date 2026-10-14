"""Branch and tag names the Hub accepts, and how they are spelled for LakeFS (#67).

LakeFS takes only letters, digits, `_` and `-` (and no leading `-`) in a branch or tag id; the Hub takes what
git takes (`feature/x`, `release/v1.0`). `to_lakefs_ref` / `from_lakefs_ref` are an exact, stateless pair.
"""

import random
import re
import string

import pytest

from kohakuhub.utils.ref_name import (
    from_lakefs_ref,
    is_valid_ref_name,
    to_lakefs_ref,
    to_lakefs_revision,
    ref_prefixes,
)

LAKEFS_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")


@pytest.mark.parametrize("name", ["main", "dev", "v1", "my_branch", "my-branch", "A_b-C", "a__b", "a--b", "a" * 200, "0123456789abcdef" * 4])
def test_a_name_lakefs_already_takes_is_left_alone(name):
    assert to_lakefs_ref(name) == name
    assert from_lakefs_ref(name) == name


@pytest.mark.parametrize(
    "name, encoded",
    [
        ("feature/x", "feature_-2F-_x"),
        ("release/v1.0", "release_-2F-_v1_-2E-_0"),
        ("a/b/c", "a_-2F-_b_-2F-_c"),
        ("-lead", "_-2D-_lead"),
        ("a_-b", "a_-5F-_-b"),  # the marker itself is escaped, so decoding is never ambiguous
        ("é", "_-E9-_"),
        ("中", "_-4E2D-_"),
    ],
)
def test_the_spelling_of_an_unsupported_name(name, encoded):
    assert to_lakefs_ref(name) == encoded
    assert from_lakefs_ref(encoded) == name
    assert LAKEFS_ID.match(encoded)


def test_every_name_comes_back_exactly_and_is_a_legal_lakefs_id():
    rng = random.Random(67)
    alphabet = string.ascii_letters + string.digits + "_-/. ~^:?*[\\@{}é中😀%+=,!#"
    seen = {}
    for _ in range(5000):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
        encoded = to_lakefs_ref(name)
        assert LAKEFS_ID.match(encoded), (name, encoded)
        assert from_lakefs_ref(encoded) == name
        assert seen.setdefault(encoded, name) == name  # no two names share an id
    for name in ["_-", "_-5F-_", "a_-2F-_b", "-_", "_-_", "_-ZZ-_", "_-2f-_"]:
        assert from_lakefs_ref(to_lakefs_ref(name)) == name


def test_no_two_names_share_an_id_over_every_short_string_of_the_marker_characters():
    import itertools

    seen = {}
    for length in range(1, 8):
        for chars in itertools.product("_-/F2", repeat=length):
            name = "".join(chars)
            encoded = to_lakefs_ref(name)
            assert from_lakefs_ref(encoded) == name, (name, encoded)
            assert seen.setdefault(encoded, name) == name, (name, encoded)


def test_a_malformed_marker_in_an_id_is_taken_as_it_is():
    # ids LakeFS made before this existed can hold `_-` without meaning an escape
    assert from_lakefs_ref("a_-zz-_b") == "a_-zz-_b"
    assert from_lakefs_ref("a_-") == "a_-"


@pytest.mark.parametrize("name", ["main", "feature/x", "release/v1.0", "a/b/c", "x-y_z", "é", "v1.0", "中文/分支", "a@b", "a.b/c.d", "{x}"])
def test_valid_ref_names(name):
    assert is_valid_ref_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "/", "-lead", "/lead", "trail/", "a//b", "a..b", "a b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "@", "a@{b", "x.lock", "a/x.lock", "a/.hidden", ".hidden", "trail.", "a/b.", "a\x00b", "a\tb", "a\x7fb"],
)
def test_names_git_refuses_are_refused(name):
    assert not is_valid_ref_name(name)


@pytest.mark.parametrize(
    "revision, expected",
    [
        ("main", "main"),
        ("main~1", "main~1"),
        ("feature/x~2", "feature_-2F-_x~2"),
        ("abc123^", "abc123^"),
        ("feature/x^2~1", "feature_-2F-_x^2~1"),
        ("feature/x", "feature_-2F-_x"),
        ("~1", "~1"),
    ],
)
def test_a_revision_expression_keeps_its_modifiers(revision, expected):
    assert to_lakefs_revision(revision) == expected


def test_the_directories_of_a_name():
    assert list(ref_prefixes("a/b/c")) == ["a", "a/b"]
    assert list(ref_prefixes("main")) == []
