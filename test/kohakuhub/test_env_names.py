"""Environment variable names: CHEESE_CAVE_* first, KOHAKU_HUB_* as the fallback."""

import os

import pytest

import kohakuhub.config as hub_config

MISSING_FILE = "/nonexistent/cheesecave-test-config.toml"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("CHEESE_CAVE_", "KOHAKU_HUB_")):
            monkeypatch.delenv(name, raising=False)
    # load_config is cached; every test must read the environment it sets up.
    hub_config.load_config.cache_clear()
    yield
    hub_config.load_config.cache_clear()


def test_cheese_cave_name_takes_precedence(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_BASE_URL", "https://old.example")
    monkeypatch.setenv("CHEESE_CAVE_BASE_URL", "https://new.example")
    assert hub_config.load_config(MISSING_FILE).app.base_url == "https://new.example"


def test_kohaku_hub_name_is_the_fallback(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_BASE_URL", "https://old.example")
    assert hub_config.load_config(MISSING_FILE).app.base_url == "https://old.example"


def test_an_empty_cheese_cave_value_still_counts_as_set(monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_SITE_NAME", "Old")
    monkeypatch.setenv("CHEESE_CAVE_SITE_NAME", "")
    assert hub_config.load_config(MISSING_FILE).app.site_name == ""


def test_numbers_and_booleans_are_parsed_from_either_name(monkeypatch):
    monkeypatch.setenv("CHEESE_CAVE_LFS_KEEP_VERSIONS", "7")
    monkeypatch.setenv("KOHAKU_HUB_LFS_AUTO_GC", "true")
    app = hub_config.load_config(MISSING_FILE).app
    assert app.lfs_keep_versions == 7
    assert app.lfs_auto_gc is True


def test_neither_name_keeps_the_default(monkeypatch):
    assert hub_config.load_config(MISSING_FILE).app.site_name == "CheeseCave"


def test_nested_sections_read_the_new_names(monkeypatch):
    monkeypatch.setenv("CHEESE_CAVE_S3_BUCKET", "bucket-new")
    monkeypatch.setenv("KOHAKU_HUB_S3_ACCESS_KEY", "key-old")
    config = hub_config.load_config(MISSING_FILE)
    assert config.s3.bucket == "bucket-new"
    assert config.s3.access_key == "key-old"
