"""Commit ids in the forms people use them (#12).

LakeFS names a commit by a 64 hex content address; the Hub, and so ``huggingface_hub``, names it by 40 hex
(a git commit id) and takes any prefix of 5 to 40. This is the syntax of that, with no database and no LakeFS.
"""

import pytest

from kohakuhub.utils.commit_id import FULL_LENGTH, HUB_LENGTH, hub_oid, is_commit_like, refers_to

FULL = "0123456789abcdef" * 4


def test_the_lengths_are_those_of_lakefs_and_of_git():
    assert (FULL_LENGTH, HUB_LENGTH) == (64, 40) and len(FULL) == FULL_LENGTH


@pytest.mark.parametrize("value", [FULL, FULL[:40], FULL[:12], FULL[:7], FULL[:5], FULL.upper(), FULL[:7].upper()])
def test_any_prefix_of_five_or_more_hex_is_a_commit_id(value):
    assert is_commit_like(value) is True


@pytest.mark.parametrize(
    "value",
    ["", "abcd", "main", "v1.0", "refs/heads/main", "g" * 40, FULL + "0", FULL[:40] + " ", "%", "a%", "ab_de", None, 7, b"abcdef"],
)
def test_nothing_else_is(value):
    assert is_commit_like(value) is False


def test_the_hub_form_is_the_first_forty_characters():
    assert hub_oid(FULL) == FULL[:40] and len(hub_oid(FULL)) == 40
    assert hub_oid(FULL[:40]) == FULL[:40]  # already the Hub's
    assert hub_oid(FULL[:7]) == FULL[:7]  # an abbreviation stays one
    assert hub_oid(None) is None and hub_oid("") == ""


@pytest.mark.parametrize("requested", [FULL, FULL[:40], FULL[:7], FULL.upper(), FULL[:40].upper(), FULL[:5]])
def test_a_request_refers_to_the_commit_it_is_a_prefix_of(requested):
    assert refers_to(requested, FULL) is True


@pytest.mark.parametrize("requested", ["0123450", "f" * 64, "f" * 40, "main", "", "%", "01%", None])
def test_and_to_no_other(requested):
    assert refers_to(requested, FULL) is False
