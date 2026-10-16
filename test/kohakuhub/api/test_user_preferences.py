"""A reader's own choices (README style) are kept on the server and follow them to every browser."""

import pytest

from kohakuhub.db import User, UserPreference
from kohakuhub.user_preferences import get_preferences, update_preferences


@pytest.fixture(autouse=True)
def no_choices_yet():
    """The tests of a module share one database; each starts with nobody having chosen anything."""
    UserPreference.delete().execute()


async def test_a_new_account_has_no_preferences(owner_client):
    response = await owner_client.get("/api/users/owner/preferences")

    assert response.status_code == 200
    assert response.json() == {}


async def test_a_choice_is_kept_and_read_back(owner_client):
    saved = await owner_client.put("/api/users/owner/preferences", json={"markdown_style": "gitlab"})

    assert saved.status_code == 200
    assert saved.json() == {"markdown_style": "gitlab"}
    assert (await owner_client.get("/api/users/owner/preferences")).json() == {"markdown_style": "gitlab"}


async def test_null_goes_back_to_following_the_site(owner_client):
    await owner_client.put("/api/users/owner/preferences", json={"markdown_style": "gitlab"})

    cleared = await owner_client.put("/api/users/owner/preferences", json={"markdown_style": None})

    assert cleared.json() == {}
    assert (await owner_client.get("/api/users/owner/preferences")).json() == {}
    assert UserPreference.select().count() == 0  # nothing is kept for a reader who follows the site


async def test_an_empty_body_changes_nothing(owner_client):
    await owner_client.put("/api/users/owner/preferences", json={"markdown_style": "mdn"})

    assert (await owner_client.put("/api/users/owner/preferences", json={})).json() == {"markdown_style": "mdn"}


async def test_a_value_that_is_not_a_style_id_is_refused(owner_client):
    for bad in ("GitHub", "", "a" * 33, "with space", 3, ["x"]):
        response = await owner_client.put("/api/users/owner/preferences", json={"markdown_style": bad})
        assert response.status_code == 422, bad
    assert (await owner_client.get("/api/users/owner/preferences")).json() == {}


async def test_an_unknown_key_is_refused(owner_client):
    response = await owner_client.put("/api/users/owner/preferences", json={"colour": "red"})

    assert response.status_code == 422


async def test_nobody_reads_or_writes_another_readers_choice(owner_client, member_client, client):
    await owner_client.put("/api/users/owner/preferences", json={"markdown_style": "mdn"})

    assert (await member_client.get("/api/users/owner/preferences")).status_code == 403
    assert (await member_client.put("/api/users/owner/preferences", json={"markdown_style": "x"})).status_code == 403
    assert (await get_preferences_for("owner")) == {"markdown_style": "mdn"}


async def test_a_guest_has_nothing_to_read_or_write(app):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as guest:
        assert (await guest.get("/api/users/owner/preferences")).status_code in (401, 403)
        assert (await guest.put("/api/users/owner/preferences", json={})).status_code in (401, 403)


async def test_whoami_carries_the_choice(owner_client):
    assert (await owner_client.get("/api/whoami-v2")).json()["preferences"] == {}

    await owner_client.put("/api/users/owner/preferences", json={"markdown_style": "vitepress"})

    assert (await owner_client.get("/api/whoami-v2")).json()["preferences"] == {"markdown_style": "vitepress"}


async def test_the_choice_goes_with_the_account(owner_client):
    await owner_client.put("/api/users/owner/preferences", json={"markdown_style": "mdn"})

    User.delete().where(User.username == "owner").execute()

    assert UserPreference.select().count() == 0


def test_a_stored_value_that_no_longer_validates_reads_as_no_choice():
    user = User.create(username="odd", normalized_name="odd")
    UserPreference.create(user=user, data='{"markdown_style": "NOT VALID", "gone": 1}')
    assert get_preferences(user) == {}

    UserPreference.update(data="not json").where(UserPreference.user == user).execute()
    assert get_preferences(user) == {}

    UserPreference.update(data="[1]").where(UserPreference.user == user).execute()
    assert get_preferences(user) == {}


def test_a_broken_row_is_replaced_by_the_next_write():
    user = User.create(username="odd2", normalized_name="odd2")
    UserPreference.create(user=user, data="not json")

    assert update_preferences(user, {"markdown_style": "mdn"}) == {"markdown_style": "mdn"}
    assert get_preferences(user) == {"markdown_style": "mdn"}


def test_clearing_where_nothing_is_kept_is_fine():
    user = User.create(username="odd3", normalized_name="odd3")

    assert update_preferences(user, {"markdown_style": None}) == {}


async def get_preferences_for(name):
    return get_preferences(User.get(User.username == name))
