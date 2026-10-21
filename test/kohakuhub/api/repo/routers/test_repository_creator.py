"""Who created a repository is recorded when it is created, and the activity feed names that person.

Everything runs against the real database and stack: the creator is the signed-in user, never the owner of
the namespace the repository lives in (an organization has no hands).
"""

import importlib

import pytest
from peewee import JOIN


def _live(module):
    return importlib.import_module(module)


def _creator(repo_type, full_id):
    db = _live("kohakuhub.db")
    row = (
        db.RepositoryCreator.select(db.RepositoryCreator, db.User)
        .join(db.Repository)
        .switch(db.RepositoryCreator)
        .join(db.User, JOIN.LEFT_OUTER)
        .where((db.Repository.full_id == full_id) & (db.Repository.repo_type == repo_type))
        .first()
    )
    return None if row is None else (row.user.username if row.user else None, row.source)


async def _created_event(client, repo_id):
    response = await client.get("/api/workspace/feed", params={"scope": "all", "limit": 100})
    assert response.status_code == 200, response.text
    return next(
        row for row in response.json()["items"] if row["kind"] == "repo_created" and row["repository"]["id"] == repo_id
    )


async def test_a_personal_repository_is_recorded_as_created_by_its_owner(owner_client):
    response = await owner_client.post("/api/repos/create", json={"type": "model", "name": "made-alone"})
    assert response.status_code == 200, response.text

    assert _creator("model", "owner/made-alone") == ("owner", "recorded")
    event = await _created_event(owner_client, "owner/made-alone")
    assert event["actor"]["username"] == "owner"
    assert event["namespace"] == {"username": "owner", "is_org": False}


@pytest.mark.parametrize("repo_type", ["model", "dataset", "space"])
async def test_an_organization_repository_is_recorded_as_created_by_the_member_who_made_it(member_client, repo_type):
    body = {"type": repo_type, "name": "made-in-org", "organization": "acme-labs"}
    if repo_type == "space":
        body["sdk"] = "gradio"
    response = await member_client.post("/api/repos/create", json=body)
    assert response.status_code == 200, response.text

    assert _creator(repo_type, "acme-labs/made-in-org") == ("member", "recorded")
    db = _live("kohakuhub.db")
    assert db.Repository.get(db.Repository.full_id == "acme-labs/made-in-org", db.Repository.repo_type == repo_type).owner.username == "acme-labs"
    event = await _created_event(member_client, "acme-labs/made-in-org")
    assert event["actor"]["username"] == "member"
    assert event["actor"]["is_org"] is False
    assert event["namespace"] == {"username": "acme-labs", "is_org": True}


async def test_a_second_create_of_the_same_name_keeps_the_first_creator(owner_client, member_client):
    first = await owner_client.post(
        "/api/repos/create", json={"type": "dataset", "name": "taken", "organization": "acme-labs"}
    )
    assert first.status_code == 200, first.text

    second = await member_client.post(
        "/api/repos/create", json={"type": "dataset", "name": "taken", "organization": "acme-labs"}
    )

    assert second.status_code == 409
    assert _creator("dataset", "acme-labs/taken") == ("owner", "recorded")


async def test_a_duplicate_is_created_by_whoever_asked_for_it(owner_client, member_client):
    source = await owner_client.post("/api/repos/create", json={"type": "model", "name": "copy-source"})
    assert source.status_code == 200, source.text

    response = await member_client.post(
        "/api/models/owner/copy-source/duplicate", json={"repository": "acme-labs/copy-of-source"}
    )

    assert response.status_code == 200, response.text
    assert _creator("model", "owner/copy-source") == ("owner", "recorded")
    assert _creator("model", "acme-labs/copy-of-source") == ("member", "recorded")


async def test_a_repository_that_could_not_be_recorded_is_not_left_half_made(owner_client, monkeypatch):
    db = _live("kohakuhub.db")

    # A deliberate database failure, injected on the one insert. A real one (dropping the table for the block)
    # holds a lock the application's other connections wait on, and the test hangs instead of failing.
    def refuse(cls, **kwargs):
        raise RuntimeError("the database said no")

    monkeypatch.setattr(db.RepositoryCreator, "create", classmethod(refuse))

    response = await owner_client.post("/api/repos/create", json={"type": "model", "name": "half-made"})

    assert response.status_code == 500
    assert "Failed to record repository" in response.headers["x-error-message"]
    assert db.Repository.get_or_none(db.Repository.full_id == "owner/half-made") is None
    assert _creator("model", "owner/half-made") is None


async def test_deleting_the_repository_deletes_its_creator_row(owner_client):
    created = await owner_client.post("/api/repos/create", json={"type": "model", "name": "short-lived"})
    assert created.status_code == 200, created.text
    assert _creator("model", "owner/short-lived") == ("owner", "recorded")

    deleted = await owner_client.request(
        "DELETE", "/api/repos/delete", json={"type": "model", "name": "short-lived"}
    )

    assert deleted.status_code == 200, deleted.text
    db = _live("kohakuhub.db")
    assert db.RepositoryCreator.select().join(db.Repository).where(db.Repository.full_id == "owner/short-lived").count() == 0


async def test_the_repository_info_names_its_creator_and_the_organization_stays_the_author(member_client):
    created = await member_client.post(
        "/api/repos/create", json={"type": "dataset", "name": "info-made", "organization": "acme-labs"}
    )
    assert created.status_code == 200, created.text

    info = (await member_client.get("/api/datasets/acme-labs/info-made")).json()

    assert info["author"] == "acme-labs"
    assert info["createdBy"] == {"username": "member", "full_name": None, "is_org": False, "avatar_url": None}


async def test_expand_created_by_answers_only_it_and_an_unknown_property_is_still_refused(member_client):
    await member_client.post("/api/repos/create", json={"type": "model", "name": "expanded", "organization": "acme-labs"})

    body = (await member_client.get("/api/models/acme-labs/expanded", params={"expand": "createdBy"})).json()
    refused = await member_client.get("/api/models/acme-labs/expanded", params={"expand": "creator"})

    assert set(body) == {"_id", "id", "createdBy"}
    assert body["createdBy"]["username"] == "member"
    assert refused.status_code == 400
    assert '"createdBy"' in refused.headers["x-error-message"]


async def test_other_expands_do_not_look_the_creator_up(owner_client):
    await owner_client.post("/api/repos/create", json={"type": "model", "name": "no-lookup"})

    body = (await owner_client.get("/api/models/owner/no-lookup", params={"expand": "likes"})).json()

    assert set(body) == {"_id", "id", "likes"}


async def test_a_repository_without_a_record_is_named_by_its_namespace_or_by_nobody(owner_client, member_client):
    db = _live("kohakuhub.db")
    await owner_client.post("/api/repos/create", json={"type": "model", "name": "legacy-mine"})
    await owner_client.post("/api/repos/create", json={"type": "model", "name": "legacy-org", "organization": "acme-labs"})
    db.RepositoryCreator.delete().execute()  # as if both were made before the creator was recorded

    mine = (await owner_client.get("/api/models/owner/legacy-mine")).json()
    org = (await owner_client.get("/api/models/acme-labs/legacy-org")).json()

    assert mine["createdBy"]["username"] == "owner"
    assert org["createdBy"] is None
    assert org["author"] == "acme-labs"


async def test_the_revision_info_names_the_creator_too(member_client):
    await member_client.post("/api/repos/create", json={"type": "model", "name": "at-revision", "organization": "acme-labs"})

    info = (await member_client.get("/api/models/acme-labs/at-revision/revision/main")).json()

    assert info["createdBy"]["username"] == "member"


async def test_the_real_client_reads_the_repository_info_with_the_extra_field(live_server_url, hf_api_token):
    from huggingface_hub import HfApi

    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    api.create_repo("owner/client-reads", repo_type="model")

    info = api.model_info("owner/client-reads")

    assert info.id == "owner/client-reads"
    assert info.author == "owner"
    assert getattr(info, "createdBy")["username"] == "owner"
