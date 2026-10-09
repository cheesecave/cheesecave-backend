"""A side branch's tree, paths-info, download and preupload read its own
identities, not the default branch's File rows (#11). Against the real LakeFS
and bucket; the database holds main's rows."""

from test.kohakuhub.api.commit.test_availability import Repo, _file, lfs
from test.kohakuhub.api.test_branch_reset import blob_sha1, sha
from test.kohakuhub.api.commit.test_availability import m  # noqa: F401 - the fixture


async def _tree(client, repo, ref):
    response = await client.get(f"/api/models/{repo.id}/tree/{ref}")
    assert response.status_code == 200, response.text
    return {item["path"]: item for item in response.json()}


async def test_a_side_branch_reads_its_own_identities(m, owner_client):
    repo = await Repo(m, owner_client, "side-reads").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(_file("README.md", "world"), lfs("w.bin", b"w dev"), branch="dev")

    dev = await _tree(owner_client, repo, "dev")
    assert dev["README.md"]["oid"] == blob_sha1("world")
    assert dev["w.bin"]["oid"] == sha(b"w dev")
    assert dev["w.bin"]["lfs"]["oid"] == sha(b"w dev")

    main = await _tree(owner_client, repo, "main")
    assert main["README.md"]["oid"] == blob_sha1("hello")
    assert main["w.bin"]["oid"] == sha(b"w main")

    response = await owner_client.post(
        f"/api/models/{repo.id}/paths-info/dev", data={"paths": ["README.md", "w.bin"]}
    )
    assert response.status_code == 200, response.text
    by_path = {entry["path"]: entry for entry in response.json()}
    assert by_path["README.md"]["oid"] == blob_sha1("world")
    assert by_path["w.bin"]["oid"] == sha(b"w dev")

    response = await owner_client.head(f"/models/{repo.id}/resolve/dev/README.md")
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == blob_sha1("world")
    response = await owner_client.head(f"/models/{repo.id}/resolve/dev/w.bin")
    assert response.headers["ETag"] == sha(b"w dev")

    # Main's content is on main: the side branch's upload of it is never skipped
    body = {"files": [{"path": "w.bin", "size": 6, "sha256": sha(b"w main")}]}
    response = await owner_client.post(f"/api/models/{repo.id}/preupload/dev", json=body)
    assert response.json()["files"][0]["shouldIgnore"] is False
    response = await owner_client.post(f"/api/models/{repo.id}/preupload/main", json=body)
    assert response.json()["files"][0]["shouldIgnore"] is True
