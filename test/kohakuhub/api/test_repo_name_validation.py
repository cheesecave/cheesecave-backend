"""Repository names: the server refuses what the Hub refuses, as the client's own rule does.

``huggingface_hub`` checks a ``repo_id`` before it sends anything (``validate_repo_id``), so a
bad name only reaches a server from a raw request or another client. Until now this one accepted
all of them: an empty name made a repository called ``owner/``, ``a/b`` one no route can reach,
a 300-character name was a 500 from the database.
"""

import asyncio

import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import HFValidationError, HfHubHTTPError, get_session, hf_raise_for_status, validate_repo_id

# Names the Hub takes. Chosen so that no two are the same once case, '-' and '_' are ignored
# (create refuses those as conflicts).
VALID = ["v", "_w", "x.y", "Foo-BAR_foo.bar123", "1.2.3", "z" * 96, "k_", "q-q"]
INVALID = [
    "",
    "z" * 97,
    "-a",
    "a-",
    ".a",
    "a.",
    "a--b",
    "a..b",
    "foo.git",
    "a b",
    "a/b",
    "a:b",
    "a@b",
    "café",
    "模型",
    "a\n",
]


async def _create(client, name, **extra):
    return await client.post("/api/repos/create", json={"type": "model", "name": name, **extra})


async def test_names_the_hub_takes_are_taken(owner_client):
    for name in VALID:
        response = await _create(owner_client, name)
        assert response.status_code == 200, (name, response.text)
        assert response.json()["repo_id"] == f"owner/{name}"


async def test_names_the_hub_refuses_are_a_400_and_make_nothing(owner_client):
    for name in INVALID:
        response = await _create(owner_client, name)
        assert response.status_code == 400, (name, response.status_code, response.text)
        assert response.headers["x-error-code"] == "InvalidRepoId"
        assert "Invalid repository name" in response.headers["x-error-message"]
    listing = (await owner_client.get("/api/models?author=owner")).json()
    names = {r["id"].split("/", 1)[1] for r in listing}
    assert not names & set(INVALID), names & set(INVALID)


async def test_a_name_in_an_organization_is_judged_the_same(owner_client):
    response = await _create(owner_client, "bad--name", organization="acme-labs")
    assert response.status_code == 400


async def test_a_rename_to_a_name_the_hub_refuses_is_a_400(owner_client):
    assert (await _create(owner_client, "rename-me")).status_code == 200
    for bad in ("owner/bad--name", "owner/a/b", "owner/-x", "owner/x.git", "owner/" + "y" * 97):
        response = await owner_client.post(
            "/api/repos/move", json={"fromRepo": "owner/rename-me", "toRepo": bad, "type": "model"}
        )
        assert response.status_code == 400, (bad, response.text)
    ok = await owner_client.post(
        "/api/repos/move", json={"fromRepo": "owner/rename-me", "toRepo": "owner/renamed.ok", "type": "model"}
    )
    assert ok.status_code == 200, ok.text


async def test_a_duplicate_to_a_name_the_hub_refuses_is_a_400_and_a_copy_of_nothing(owner_client):
    assert (await _create(owner_client, "dup-bad-name-src")).status_code == 200
    for bad in ("owner/a--b", "owner/foo.git", "owner/a b"):
        response = await owner_client.post("/api/models/owner/dup-bad-name-src/duplicate", json={"repository": bad})
        assert response.status_code == 400, (bad, response.text)


@pytest.mark.hf_client
async def test_the_installed_client_and_the_server_agree_on_names(owner_client):
    """Whatever the installed ``huggingface_hub`` accepts of the names the Hub takes, the server takes;
    whatever the client refuses of the others, the server refuses too (older clients are laxer about
    non-ASCII names, which the server still refuses: the Hub's names are ASCII)."""

    def refused_by_client(name):
        try:
            validate_repo_id(f"owner/{name}")
        except HFValidationError:
            return True
        return False

    for name in VALID:
        assert not refused_by_client(name), f"the client refuses {name!r}, which the Hub takes"
        # (created by test_names_the_hub_takes_are_taken in this module, or here: a conflict is fine)
        assert (await _create(owner_client, name)).status_code in (200, 409), name
    for name in INVALID:
        if refused_by_client(name):
            assert (await _create(owner_client, name)).status_code == 400, name


@pytest.mark.hf_client
async def test_hf_client_reads_the_refusal_as_a_400_and_still_creates_good_names(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    def run():
        assert str(api.create_repo("owner/hf.name_ok-1", exist_ok=True)).endswith("owner/hf.name_ok-1")
        # The client would stop these before sending: send them as a raw client would
        headers = api._build_hf_headers()
        for bad in ("a--b", "a/b", "x" * 97, "foo.git"):
            response = get_session().post(
                f"{live_server_url}/api/repos/create", headers=headers, json={"type": "model", "name": bad}
            )
            with pytest.raises(HfHubHTTPError) as refused:
                hf_raise_for_status(response)
            assert refused.value.response.status_code == 400, bad
        with pytest.raises(HFValidationError):  # and the client's own rule says the same
            api.create_repo("owner/a--b")

    await asyncio.to_thread(run)
