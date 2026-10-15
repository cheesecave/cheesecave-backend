"""API tests for branch and tag routes."""

from test.kohakuhub.api.helpers import encode_ndjson, file_op


async def test_create_branch_commit_and_delete(owner_client):
    create_response = await owner_client.post(
        "/api/models/owner/demo-model/branch",
        json={"branch": "feature"},
    )
    assert create_response.status_code == 200

    commit_payload = encode_ndjson(
        [
            {"key": "header", "value": {"summary": "Branch change"}},
            file_op("branch-only.txt", b"branch specific content"),
        ]
    )
    commit_response = await owner_client.post(
        "/api/models/owner/demo-model/commit/feature",
        content=commit_payload,
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert commit_response.status_code == 200

    feature_tree = await owner_client.get(
        "/api/models/owner/demo-model/tree/feature",
        params={"recursive": "true"},
    )
    assert feature_tree.status_code == 200
    assert any(item["path"] == "branch-only.txt" for item in feature_tree.json())

    main_tree = await owner_client.get(
        "/api/models/owner/demo-model/tree/main",
        params={"recursive": "true"},
    )
    assert main_tree.status_code == 200
    assert all(item["path"] != "branch-only.txt" for item in main_tree.json())

    delete_response = await owner_client.delete(
        "/api/models/owner/demo-model/branch/feature"
    )
    assert delete_response.status_code == 200


async def test_tag_lifecycle_and_main_branch_protection(owner_client):
    create_response = await owner_client.post(
        "/api/models/owner/demo-model/tag",
        json={"tag": "release-1"},
    )
    assert create_response.status_code == 200

    tree_response = await owner_client.get("/api/models/owner/demo-model/tree/release-1")
    assert tree_response.status_code == 200
    assert any(item["path"] == "README.md" for item in tree_response.json())

    main_delete_response = await owner_client.delete(
        "/api/models/owner/demo-model/branch/main"
    )
    assert main_delete_response.status_code == 400
    assert "Cannot delete main branch" in main_delete_response.headers["x-error-message"]

    delete_response = await owner_client.delete(
        "/api/models/owner/demo-model/tag/release-1"
    )
    assert delete_response.status_code == 200


async def test_non_writer_cannot_create_branch(visitor_client):
    response = await visitor_client.post(
        "/api/models/owner/demo-model/branch",
        json={"branch": "forbidden"},
    )

    assert response.status_code == 403


async def test_deleting_a_missing_branch_or_tag_is_a_404_revision_not_found(owner_client):
    """A branch or tag that is not there (never was, or already deleted) is a 404, not a 500."""
    response = await owner_client.post("/api/repos/create", json={"type": "model", "name": "delete-missing-ref"})
    assert response.status_code == 200, response.text
    base = "/api/models/owner/delete-missing-ref"
    assert (await owner_client.post(f"{base}/branch", json={"branch": "once", "revision": "main"})).status_code == 200
    assert (await owner_client.post(f"{base}/tag", json={"tag": "v-once", "revision": "main"})).status_code == 200

    for kind, name in (("branch", "once"), ("tag", "v-once")):
        assert (await owner_client.delete(f"{base}/{kind}/{name}")).status_code == 200
        for gone in (name, f"never-{name}"):  # already deleted, and never there
            missing = await owner_client.delete(f"{base}/{kind}/{gone}")
            assert missing.status_code == 404, (kind, gone, missing.text)
            assert missing.headers["x-error-code"] == "RevisionNotFound"
            assert f"Revision '{gone}' not found" in missing.headers["x-error-message"]
    # What is guarded stays guarded
    assert (await owner_client.delete(f"{base}/branch/main")).status_code == 400
