"""Commit ids in the forms people use them (#12).

LakeFS names a commit by a 64 hex content address; the Hub, and so ``huggingface_hub``, names it by 40 hex
(a git commit id) and takes any prefix of 5 to 40. This is the syntax of that, with no database and no LakeFS.
"""

import pytest

import importlib

from kohakuhub.utils.commit_id import (
    FULL_LENGTH,
    HUB_LENGTH,
    hub_metadata,
    hub_oid,
    hub_oids,
    is_commit_like,
    refers_to,
)

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


# ----- the form the site says (commit_id_format) -----


@pytest.fixture
def live_cfg():
    """The registered config (the harness reloads modules between sessions)."""
    return importlib.import_module("kohakuhub.config").cfg


def test_the_default_form_is_the_hubs(live_cfg):
    assert live_cfg.app.commit_id_format == "hub"
    assert hub_oid(FULL) == FULL[:40]


def test_the_full_form_says_the_commit_as_lakefs_does(live_cfg, monkeypatch):
    """The transitional form: every place that says a commit says all 64, none says 40."""
    monkeypatch.setattr(live_cfg.app, "commit_id_format", "full")
    assert hub_oid(FULL) == FULL
    assert hub_oid(FULL[:7]) == FULL[:7] and hub_oid(None) is None and hub_oid("") == ""


def test_a_list_of_ids_is_said_in_the_form_of_the_site(live_cfg, monkeypatch):
    assert hub_oids([FULL, FULL[:7]]) == [FULL[:40], FULL[:7]] and hub_oids([]) == [] and hub_oids(None) == []
    monkeypatch.setattr(live_cfg.app, "commit_id_format", "full")
    assert hub_oids([FULL, FULL[:7]]) == [FULL, FULL[:7]]


def test_the_ids_in_the_metadata_of_a_commit_are_said_as_the_site_says_them(live_cfg, monkeypatch):
    """A revert writes the commit it undid as a full id (``revert_of``); what a person wrote is left as it is."""
    metadata = {
        "revert_of": FULL,
        "email": "someone@example.com",
        "short": FULL[:7],
        "not an id": "z" * 64,
        "number": 7,
        "free text": f"see {FULL}",
    }
    said = hub_metadata(metadata)
    assert said["revert_of"] == FULL[:40]
    assert {k: v for k, v in said.items() if k != "revert_of"} == {k: v for k, v in metadata.items() if k != "revert_of"}
    assert hub_metadata(None) == {} and hub_metadata({}) == {}
    monkeypatch.setattr(live_cfg.app, "commit_id_format", "full")
    assert hub_metadata(metadata) == metadata
