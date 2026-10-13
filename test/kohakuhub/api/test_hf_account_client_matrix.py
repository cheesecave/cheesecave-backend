"""Follow lists, overviews, organization members, ``auth_check`` and tag vocabularies, read by ``huggingface_hub``.

Run against whichever client version is installed (the CI matrix installs 0.20.3 .. latest).
A call the version does not have is skipped. Whole app over HTTP, real database and LakeFS.
"""

from __future__ import annotations

import asyncio
import sys
import warnings

import pytest

pytestmark = [pytest.mark.backend_per_test, pytest.mark.hf_client]


def _hf():
    import huggingface_hub

    return huggingface_hub


def _need(api, name):
    if not hasattr(api, name):
        pytest.skip(f"huggingface_hub {_hf().__version__} has no HfApi.{name}")
    return getattr(api, name)


async def _run(function, *args, **kwargs):
    def call():
        with warnings.catch_warnings():  # the suite turns warnings into errors; a client's own notices are not ours
            warnings.simplefilter("ignore", FutureWarning)
            return function(*args, **kwargs)

    return await asyncio.to_thread(call)


async def _listing(api, name, *args):
    return await _run(lambda: list(_need(api, name)(*args)))


def _db():
    return sys.modules["kohakuhub.db"]


def _name_of(entry):
    """The account name of a list entry, whichever attribute this client version uses."""
    return getattr(entry, "username", None) or getattr(entry, "user", None) or entry["user"]


@pytest.fixture(autouse=True)
def global_endpoint(live_server_url, monkeypatch):
    """Some client calls (the follow lists, overviews) read the module-level ``constants.ENDPOINT`` and ignore
    ``HfApi(endpoint=...)``; without this they would go to huggingface.co."""
    import importlib

    for name in ("huggingface_hub.constants", "huggingface_hub.hf_api"):
        module = importlib.import_module(name)
        if hasattr(module, "ENDPOINT"):
            monkeypatch.setattr(module, "ENDPOINT", live_server_url)


@pytest.fixture
def api(live_server_url, hf_api_token):
    return _hf().HfApi(endpoint=live_server_url, token=hf_api_token)


@pytest.fixture
def anonymous(live_server_url):
    return _hf().HfApi(endpoint=live_server_url, token=False)


async def _follow(follower_client, target):
    (await follower_client.put(f"/api/users/{target}/follow")).raise_for_status()


async def _organization(owner_client, name="clientorg"):
    (await owner_client.post("/org/create", json={"name": name})).raise_for_status()
    return name


# --- follow lists ----------------------------------------------------------


async def test_followers_and_following_are_read_by_the_client(api, owner_client, outsider_client):
    await _follow(outsider_client, "owner")  # outsider follows owner

    followers = await _listing(api, "list_user_followers", "owner")
    following = await _listing(api, "list_user_following", "outsider")

    assert [_name_of(f) for f in followers] == ["outsider"]
    assert [_name_of(f) for f in following] == ["owner"]


async def test_a_follow_list_longer_than_a_page_is_followed_through_link(api, owner_client):
    db = _db()
    for index in range(130):
        follower = db.User.create(username=f"fan{index:03d}", normalized_name=f"fan{index:03d}", email=f"fan{index}@test.invalid")
        db.UserFollow.create(follower=follower, followed=db.User.get(db.User.username == "owner"))

    names = [_name_of(f) for f in await _listing(api, "list_user_followers", "owner")]

    assert len(names) == len(set(names)) == 130


async def test_organization_followers_are_read_by_the_client(api, owner_client, outsider_client):
    org = await _organization(owner_client)
    await _follow(outsider_client, org)

    followers = await _listing(api, "list_organization_followers", org)

    assert [_name_of(f) for f in followers] == ["outsider"]


# --- overviews and members -------------------------------------------------


async def test_the_user_overview_is_read_by_the_client(api, owner_client, outsider_client):
    await _follow(outsider_client, "owner")

    overview = await _run(_need(api, "get_user_overview"), "owner")

    assert overview.username == "owner" and overview.num_followers == 1
    assert overview.num_models >= 0 and overview.num_datasets >= 0 and overview.num_spaces >= 0


async def test_the_organization_overview_and_members_are_read_by_the_client(api, owner_client):
    org = await _organization(owner_client)

    overview = await _run(_need(api, "get_organization_overview"), org)
    members = await _listing(api, "list_organization_members", org)

    assert overview.name == org and overview.num_users == 1
    assert [_name_of(m) for m in members] == ["owner"]


async def test_the_overview_of_a_missing_account_is_not_found(api):
    with pytest.raises(Exception) as failed:
        await _run(_need(api, "get_user_overview"), "no-such-user")

    assert "404" in str(failed.value)


# --- whoami ----------------------------------------------------------------


async def test_whoami_carries_the_avatar_and_the_organization_fields(api, owner_client):
    await _organization(owner_client)

    me = await _run(api.whoami)

    assert me["name"] == "owner" and "avatarUrl" in me
    org = next(o for o in me["orgs"] if o["name"] == "clientorg")
    assert org["type"] == "org" and org["roleInOrg"] and "avatarUrl" in org


# --- auth_check ------------------------------------------------------------


@pytest.mark.parametrize("repo_type", ["model", "dataset", "space"])
async def test_auth_check_passes_for_a_readable_repository(api, anonymous, owner_client, repo_type):
    (await owner_client.post("/api/repos/create", json={"type": repo_type, "name": "open", "private": False})).raise_for_status()

    assert await _run(_need(api, "auth_check"), "owner/open", repo_type=repo_type) is None
    assert await _run(_need(anonymous, "auth_check"), "owner/open", repo_type=repo_type) is None


@pytest.mark.parametrize("repo_type", ["model", "dataset", "space"])
async def test_auth_check_raises_repository_not_found_for_a_private_or_missing_repository(
    anonymous, owner_client, live_server_url, repo_type
):
    from huggingface_hub.utils import RepositoryNotFoundError

    (await owner_client.post("/api/repos/create", json={"type": repo_type, "name": "closed", "private": True})).raise_for_status()
    check = _need(anonymous, "auth_check")

    with pytest.raises(RepositoryNotFoundError):
        await _run(check, "owner/closed", repo_type=repo_type)
    with pytest.raises(RepositoryNotFoundError):
        await _run(check, "owner/never-created", repo_type=repo_type)


async def test_auth_check_passes_for_the_owner_of_a_private_repository(api, owner_client):
    (await owner_client.post("/api/repos/create", json={"type": "model", "name": "mine", "private": True})).raise_for_status()

    assert await _run(_need(api, "auth_check"), "owner/mine") is None


# --- tag vocabularies --------------------------------------------------------


async def test_the_tag_vocabularies_are_read_by_the_client(api, owner_client):
    (await owner_client.post("/api/repos/create", json={"type": "model", "name": "tagged", "private": False})).raise_for_status()
    db = _db()
    repo = db.Repository.get((db.Repository.namespace == "owner") & (db.Repository.name == "tagged"))
    db.RepositoryFacet.create(repository=repo, key="task", value="fill-mask")
    db.RepositoryMetadata.get_or_create(repository=repo, defaults={"state": "ready"})
    db.RepositoryMetadata.update(state="ready").where(db.RepositoryMetadata.repository == repo).execute()

    model_tags = await _run(_need(api, "get_model_tags"))
    dataset_tags = await _run(_need(api, "get_dataset_tags"))

    assert "fill-mask" in str(model_tags) and "pipeline_tag" in str(model_tags)
    assert "task_categories" in str(dataset_tags)
