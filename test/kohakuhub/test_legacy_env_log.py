"""At startup the application advises renaming variables that still use the old prefix.

Names only, never values; nothing is logged when every name is already the new one.
"""

import importlib
import os

import pytest


@pytest.fixture
def started(monkeypatch):
    """Start the lifespan with the process environment this test sets, return what it warned."""
    main_mod = importlib.import_module("kohakuhub.main")
    monkeypatch.setattr(main_mod, "init_storage", lambda: None)
    compat = importlib.import_module("kohakuhub.lakefs_compat")

    async def _learn():
        return None

    monkeypatch.setattr(compat, "learn", _learn)
    lines = []
    monkeypatch.setattr(main_mod.logger, "warning", lines.append)

    async def run():
        async with main_mod.lifespan(object()):
            pass
        return lines

    return run


async def test_an_old_prefixed_variable_is_advised_about_by_name(started, monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_SITE_NAME", "zzz-value-not-to-be-logged")

    lines = await started()

    advice = [line for line in lines if "old KOHAKU_HUB_ prefix" in line]
    assert len(advice) == 1
    assert "KOHAKU_HUB_SITE_NAME -> CHEESE_CAVE_SITE_NAME" in advice[0]
    assert all("zzz-value-not-to-be-logged" not in line for line in lines)


async def test_a_name_set_under_both_prefixes_says_the_old_one_is_ignored(started, monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_SITE_NAME", "old")
    monkeypatch.setenv("CHEESE_CAVE_SITE_NAME", "new")

    lines = await started()

    assert any("both prefixes" in line and "KOHAKU_HUB_SITE_NAME" in line for line in lines)


async def test_nothing_is_logged_when_every_name_is_new(started, monkeypatch):
    monkeypatch.setenv("CHEESE_CAVE_SITE_NAME", "new")
    for name in [n for n in os.environ if n.startswith("KOHAKU_HUB_")]:
        monkeypatch.delenv(name)

    lines = await started()

    assert not any("prefix" in line for line in lines)
