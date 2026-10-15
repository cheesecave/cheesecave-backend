"""Tests for name normalization helpers."""

import pytest

from kohakuhub.utils.names import normalize_name, repo_name_problem


def test_normalize_name_lowercases_and_strips_separators():
    assert normalize_name("My-Repo_Name") == "myreponame"


def test_normalize_name_preserves_other_characters():
    assert normalize_name("Model.v2") == "model.v2"


@pytest.mark.parametrize(
    "name",
    ["a", "A", "0", "_x", "x_", "a.b", "a-b", "a_b", "Foo-BAR_foo.bar123", "1.2.3", "a.git.b", "x" * 96],
)
def test_repo_name_problem_accepts_what_the_hub_accepts(name):
    assert repo_name_problem(name) is None


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("", "1 to 96"),
        ("x" * 97, "1 to 96"),
        ("-a", "start or end"),
        ("a-", "start or end"),
        (".a", "start or end"),
        ("a.", "start or end"),
        ("a b", "letters, digits"),
        ("a/b", "letters, digits"),
        ("a:b", "letters, digits"),
        ("a\n", "letters, digits"),  # a trailing newline is no character of the name
        ("café", "letters, digits"),  # the Hub's names are ASCII
        ("模型", "letters, digits"),
        ("a--b", "'--' or '..'"),
        ("a..b", "'--' or '..'"),
        ("foo.git", "'.git'"),
    ],
)
def test_repo_name_problem_says_what_is_wrong(name, why):
    assert why in repo_name_problem(name)
