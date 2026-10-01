"""API tests for repository info routes."""

import httpx


async def test_get_repo_info_returns_siblings_and_lfs_metadata(client):
    response = await client.get("/api/models/owner/demo-model", params={"blobs": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "owner/demo-model"
    sibling_names = {item["rfilename"] for item in payload["siblings"]}
    assert {"README.md", "config.json", "weights/model.safetensors"} <= sibling_names

    lfs_sibling = next(
        sibling for sibling in payload["siblings"] if sibling["rfilename"] == "weights/model.safetensors"
    )
    assert lfs_sibling["lfs"]["size"] > 0


async def test_list_repositories_and_user_repo_views_respect_visibility(
    app, client, owner_client
):
    model_list_response = await client.get("/api/models", params={"author": "owner"})
    assert model_list_response.status_code == 200
    assert any(repo["id"] == "owner/demo-model" for repo in model_list_response.json())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as anonymous_client:
        anonymous_user_repos = await anonymous_client.get("/api/users/acme-labs/repos")
        assert anonymous_user_repos.status_code == 200
        assert anonymous_user_repos.json()["datasets"] == []

    owner_user_repos = await owner_client.get("/api/users/acme-labs/repos")
    assert owner_user_repos.status_code == 200
    assert any(
        repo["id"] == "acme-labs/private-dataset"
        for repo in owner_user_repos.json()["datasets"]
    )


async def test_user_overview_endpoint_groups_by_type(owner_client):
    """``repoAPI.getUserOverview`` in kohaku-hub-ui drives the user profile
    page — one round trip must return models/datasets/spaces in a single
    grouped payload. Uses a large ``limit`` so transient repos from sibling
    tests do not push the baseline seed out of the ``recent`` window."""
    response = await owner_client.get(
        "/api/users/owner/repos", params={"sort": "recent", "limit": 200}
    )
    response.raise_for_status()
    payload = response.json()
    for key in ("models", "datasets", "spaces"):
        assert key in payload, f"user overview must include {key!r}, got {payload!r}"
        assert isinstance(payload[key], list)
    assert any(repo["id"] == "owner/demo-model" for repo in payload["models"])


def _pointer(sha256: str, size: int) -> bytes:
    return f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha256}\nsize {size}\n".encode()


def _git_blob_id(content: bytes) -> str:
    import hashlib

    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


async def test_get_repo_info_default_lists_names_only_like_the_hub(client):
    """Without ``blobs``, the Hub lists ``rfilename`` only, and
    ``huggingface_hub`` sends no ``blobs`` unless ``files_metadata=True``."""
    response = await client.get("/api/models/owner/demo-model")

    assert response.status_code == 200
    siblings = response.json()["siblings"]
    assert {"README.md", "weights/model.safetensors"} <= {s["rfilename"] for s in siblings}
    assert all(set(sibling) == {"rfilename"} for sibling in siblings)


async def test_get_repo_info_blobs_false_returns_names_only(client):
    response = await client.get("/api/models/owner/demo-model", params={"blobs": "false"})

    assert response.status_code == 200
    assert all(set(s) == {"rfilename"} for s in response.json()["siblings"])


async def test_get_repo_info_blobs_true_carries_the_hub_blob_fields(client):
    """``blobs=true`` (``files_metadata=True``): every file has ``blobId`` and
    ``size``; an LFS file adds ``lfs``, read from the object LakeFS links, and
    its ``blobId`` is the git blob id of its pointer file, as on the Hub."""
    from kohakuhub.db import File
    from kohakuhub.db_operations import get_repository

    response = await client.get("/api/models/owner/demo-model", params={"blobs": "true"})

    assert response.status_code == 200
    siblings = {s["rfilename"]: s for s in response.json()["siblings"]}
    repo = get_repository("model", "owner", "demo-model")
    rows = {f.path_in_repo: f for f in File.select().where(File.repository == repo)}

    readme = siblings["README.md"]
    assert set(readme) == {"rfilename", "blobId", "size"}
    assert readme["blobId"] == rows["README.md"].sha256  # its git blob id
    assert readme["size"] == rows["README.md"].size

    weights = siblings["weights/model.safetensors"]
    sha256, size = rows["weights/model.safetensors"].sha256, rows["weights/model.safetensors"].size
    assert weights["lfs"] == {"sha256": sha256, "size": size, "pointerSize": len(_pointer(sha256, size))}
    assert weights["blobId"] == _git_blob_id(_pointer(sha256, size))
    assert weights["size"] == size


async def test_get_repo_info_expand_returns_only_what_was_asked(client):
    response = await client.get(
        "/api/models/owner/demo-model", params=[("expand", "sha"), ("expand", "lastModified")]
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"_id", "id", "sha", "lastModified"}
    assert len(body["sha"]) == 40 and body["id"] == "owner/demo-model"


async def test_get_repo_info_expand_siblings_and_blobs(client):
    names = await client.get("/api/models/owner/demo-model", params={"expand": "siblings"})
    with_blobs = await client.get(
        "/api/models/owner/demo-model", params={"expand": "sha", "blobs": "true"}
    )

    assert set(names.json()) == {"_id", "id", "siblings"}
    assert all(set(s) == {"rfilename"} for s in names.json()["siblings"])
    # As on the Hub, blobs=true adds the detailed file list to an expand
    assert set(with_blobs.json()) == {"_id", "id", "sha", "siblings"}
    assert all("blobId" in s for s in with_blobs.json()["siblings"])


async def test_get_repo_info_expand_page_fields(owner_client):
    """The fields the repository page asks for, KohakuHub's ``storage`` included."""
    fields = ["sha", "lastModified", "createdAt", "private", "downloads", "likes",
              "tags", "usedStorage", "gated", "author", "disabled", "storage"]
    response = await owner_client.get(
        "/api/models/owner/demo-model", params=[("expand", f) for f in fields]
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"_id", "id", *fields}
    assert body["private"] is False and body["author"] == "owner"
    assert isinstance(body["usedStorage"], int) and body["usedStorage"] > 0
    assert body["storage"]["used_bytes"] == body["usedStorage"]


async def test_get_repo_info_expand_rejects_an_unknown_property(client):
    response = await client.get("/api/models/owner/demo-model", params={"expand": "bogus"})

    assert response.status_code == 400
    assert "bogus" in response.headers["x-error-message"]
    # Properties of another repository type are unknown too
    dataset_only = await client.get("/api/models/owner/demo-model", params={"expand": "citation"})
    assert dataset_only.status_code == 400


async def test_revision_info_follows_the_same_contract(client):
    base = "/api/models/owner/demo-model/revision/main"
    default = await client.get(base)
    expanded = await client.get(base, params=[("expand", "sha"), ("expand", "lastModified")])
    blobs = await client.get(base, params={"blobs": "true"})
    invalid = await client.get(base, params={"expand": "bogus"})

    assert default.status_code == 200
    assert "_id" in default.json()
    assert all(set(s) == {"rfilename"} for s in default.json()["siblings"])
    assert set(expanded.json()) == {"_id", "id", "sha", "lastModified"}
    assert all("blobId" in s for s in blobs.json()["siblings"])
    assert invalid.status_code == 400


async def test_an_empty_expand_is_no_expand_like_the_hub(client):
    """``?expand=`` answers everything on the Hub, as without it."""
    for path in ("/api/models/owner/demo-model", "/api/models/owner/demo-model/revision/main"):
        response = await client.get(path, params={"expand": ""})

        assert response.status_code == 200
        assert "siblings" in response.json() and "private" in response.json()
