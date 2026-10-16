"""The shared reader of environment names: CHEESE_CAVE_<NAME> first, KOHAKU_HUB_<NAME> as the fallback.

``kohakuhub.envnames`` has no side effects on import, so the container entrypoint and the
scripts can use it without loading (and validating) the application's configuration.
"""

import pytest

from kohakuhub import config, envnames


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith(("CHEESE_CAVE_", "KOHAKU_HUB_")):
            monkeypatch.delenv(name, raising=False)


def test_the_new_prefix_wins_over_the_old_one(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_X", "old")
    monkeypatch.setenv("CHEESE_CAVE_X", "new")
    assert envnames.read_env("X") == "new"


def test_the_old_prefix_is_the_fallback(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_X", "old")
    assert envnames.read_env("X") == "old"


def test_nothing_set_is_none():
    assert envnames.read_env("X") is None


def test_an_empty_value_still_counts_as_set(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_X", "old")
    monkeypatch.setenv("CHEESE_CAVE_X", "")
    assert envnames.read_env("X") == ""


def test_has_env_sees_either_prefix(monkeypatch):
    assert envnames.has_env("X") is False
    monkeypatch.setenv("KOHAKU_HUB_X", "")
    assert envnames.has_env("X") is True
    monkeypatch.delenv("KOHAKU_HUB_X")
    monkeypatch.setenv("CHEESE_CAVE_X", "1")
    assert envnames.has_env("X") is True


def test_env_or_gives_the_default_only_when_unset(monkeypatch):
    assert envnames.env_or("X", "dflt") == "dflt"
    monkeypatch.setenv("KOHAKU_HUB_X", "")
    assert envnames.env_or("X", "dflt") == ""
    monkeypatch.setenv("CHEESE_CAVE_X", "new")
    assert envnames.env_or("X", "dflt") == "new"


def test_read_env_nonempty_skips_an_empty_value_to_the_next_prefix(monkeypatch):
    # An image build declares both names; one that was not passed is empty, not absent.
    monkeypatch.setenv("CHEESE_CAVE_GIT_SHA", "")
    monkeypatch.setenv("KOHAKU_HUB_GIT_SHA", "abc123")
    assert envnames.read_env_nonempty("GIT_SHA") == "abc123"
    monkeypatch.setenv("CHEESE_CAVE_GIT_SHA", "def456")
    assert envnames.read_env_nonempty("GIT_SHA") == "def456"
    monkeypatch.setenv("CHEESE_CAVE_GIT_SHA", "")
    monkeypatch.setenv("KOHAKU_HUB_GIT_SHA", "")
    assert envnames.read_env_nonempty("GIT_SHA") is None


def test_config_keeps_exporting_the_same_names():
    assert config.read_env is envnames.read_env
    assert config.ENV_PREFIXES == envnames.ENV_PREFIXES == ("CHEESE_CAVE_", "KOHAKU_HUB_")


def test_legacy_names_in_use_lists_only_the_old_names_that_still_take_effect():
    environ = {
        "KOHAKU_HUB_ONLY_OLD": "1",
        "KOHAKU_HUB_BOTH": "old",
        "CHEESE_CAVE_BOTH": "new",
        "CHEESE_CAVE_ONLY_NEW": "1",
        "PATH": "/usr/bin",
    }
    in_use, shadowed = envnames.legacy_names(environ)
    assert in_use == ["KOHAKU_HUB_ONLY_OLD"]
    assert shadowed == ["KOHAKU_HUB_BOTH"]


def test_legacy_names_ignores_what_the_system_itself_writes_under_the_old_prefix():
    environ = {
        "KOHAKU_HUB_LAKEFS_ACCESS_KEY": "k",
        "KOHAKU_HUB_LAKEFS_SECRET_KEY": "s",
        "KOHAKU_HUB_GIT_SHA": "abc",
        "KOHAKU_HUB_BUILD_TIME": "t",
    }
    assert envnames.legacy_names(environ) == ([], [])


def test_legacy_names_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_SOMETHING", "v")
    assert envnames.legacy_names()[0] == ["KOHAKU_HUB_SOMETHING"]


def test_the_advice_names_the_variables_but_never_their_values(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_SESSION_SECRET", "super-secret-value")
    monkeypatch.setenv("KOHAKU_HUB_SITE_NAME", "zzz-old-value")
    monkeypatch.setenv("CHEESE_CAVE_SITE_NAME", "zzz-new-value")
    lines = envnames.legacy_advice()
    text = "\n".join(lines)
    assert "KOHAKU_HUB_SESSION_SECRET" in text and "CHEESE_CAVE_SESSION_SECRET" in text
    assert "KOHAKU_HUB_SITE_NAME" in text and "ignored" in text
    for value in ("super-secret-value", "zzz-old-value", "zzz-new-value"):
        assert value not in text


def test_no_advice_when_every_name_is_new(monkeypatch):
    monkeypatch.setenv("CHEESE_CAVE_SITE_NAME", "New")
    assert envnames.legacy_advice() == []
