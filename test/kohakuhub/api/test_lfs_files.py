"""`list_lfs_files` lists a repository's LFS objects; `permanently_delete_lfs_files` is not supported (#66).

`GET /api/{type}s/{repo}/lfs-files` lists what a repository stores as LFS; `POST .../lfs-files/batch` is answered `501`
whatever it asks, and deletes nothing. Real database and bucket (the shared `m` fixture of `test_lfs_gc.py`).
"""

import pytest

# the fixture and helpers of the garbage collection tests: same real database, bucket and commits
from test.kohakuhub.test_lfs_gc import (  # noqa: F401
    _age_recent,
    _commit,
    _lfs_op,
    _put,
    _repo,
    _sha,
    _stored,
    m,
)

B = "/api/models/owner/demo-model"
LIST = f"{B}/lfs-files"
BATCH = f"{B}/lfs-files/batch"


async def _versions(m, client, count, prefix="lfs-files-test"):
    """`count` versions of one path, committed oldest first; returns their contents and oids."""
    contents = [f"{prefix} version {i} ".encode() * 20 for i in range(count)]
    oids = [_put(m, content) for content in contents]
    for content in contents:
        assert (await _commit(client, "demo-model", _lfs_op(content, path="ckpt/model.bin"))).status_code == 200
    return contents, oids


async def _ours(client, url=LIST):
    """The entries of this module's paths: the seeded repository has an LFS file of its own."""
    return [e for e in (await client.get(url)).json() if e["filename"].startswith("ckpt/")]


@pytest.fixture(autouse=True)
def clean(m):
    yield
    H = m.db.LFSObjectHistory
    H.delete().where(H.path_in_repo.startswith("ckpt/")).execute()
    m.db.File.delete().where(m.db.File.path_in_repo.startswith("ckpt/")).execute()


# ----- the list -----


async def test_a_repository_without_lfs_files_lists_nothing(m, owner_client):
    made = await owner_client.post("/api/repos/create", json={"type": "model", "name": "no-lfs"})
    assert made.status_code == 200, made.text
    response = await owner_client.get("/api/models/owner/no-lfs/lfs-files")
    assert response.status_code == 200
    assert response.json() == [] and "link" not in response.headers


async def test_the_seeded_lfs_file_is_listed(m, owner_client):
    entries = (await owner_client.get(LIST)).json()
    assert [e["filename"] for e in entries] == ["weights/model.safetensors"]


async def test_each_object_is_listed_once_with_the_fields_the_client_reads(m, owner_client):
    contents, oids = await _versions(m, owner_client, 3)
    assert (await owner_client.get(LIST)).status_code == 200
    entries = await _ours(owner_client)
    assert [e["oid"] for e in entries] == oids[::-1]  # newest first
    first = entries[-1]
    assert set(first) >= {"filename", "oid", "fileOid", "pushedAt", "ref", "size"}
    assert first["filename"] == "ckpt/model.bin" and first["size"] == len(contents[0])
    assert first["fileOid"] == first["oid"] == oids[0]  # what the client sends back to delete
    assert first["ref"] == "refs/heads/main"
    assert first["pushedAt"].endswith("Z")


async def test_the_same_content_at_two_paths_is_one_object(m, owner_client):
    content = b"shared content " * 40
    oid = _put(m, content)
    for path in ("ckpt/a.bin", "ckpt/b.bin"):
        assert (await _commit(owner_client, "demo-model", _lfs_op(content, path=path))).status_code == 200
    assert [e["oid"] for e in await _ours(owner_client)] == [oid]


async def test_an_object_garbage_collection_deleted_is_not_listed(m, owner_client):
    _, oids = await _versions(m, owner_client, 2)
    # a tombstone is what says the collection deleted it
    m.db.LfsObjectTombstone.create(sha256=oids[0], state=m.gc.DELETED)
    assert [e["oid"] for e in await _ours(owner_client)] == [oids[1]]


async def test_the_list_is_paged_with_a_link_header(m, owner_client):
    _, oids = await _versions(m, owner_client, 5)
    seen, url = [], f"{LIST}?limit=2"
    while url:
        page = await owner_client.get(url)
        assert page.status_code == 200 and len(page.json()) <= 2
        seen += [e["oid"] for e in page.json() if e["filename"].startswith("ckpt/")]
        link = page.headers.get("link")
        url = link.split("<", 1)[1].split(">", 1)[0].replace("http://testserver", "") if link else None
        if url and not url.startswith("/api/"):
            url = "/api" + url[url.index("/models/") :]
    assert seen == oids[::-1]


async def test_a_bad_limit_is_refused(m, owner_client):
    assert (await owner_client.get(f"{LIST}?limit=0")).status_code == 422
    assert (await owner_client.get(f"{LIST}?cursor=not-a-cursor")).status_code == 400


async def test_listing_needs_write_access_to_the_repository(m, visitor_client, member_client):
    assert (await visitor_client.get(LIST)).status_code in (403, 404)
    private = await visitor_client.get("/api/datasets/acme-labs/private-dataset/lfs-files")
    assert private.status_code in (403, 404)


async def test_a_repository_that_does_not_exist_says_so(m, owner_client):
    missing = await owner_client.get("/api/models/owner/nope/lfs-files")
    assert missing.status_code == 404 and missing.headers["x-error-code"] == "RepoNotFound"


async def test_a_version_no_file_or_commit_row_names_has_no_ref(m, owner_client):
    """History written before commits were recorded per branch names no ref."""
    sha = _sha("an old version")
    m.db.LFSObjectHistory.create(
        repository=_repo(m), path_in_repo="ckpt/old.bin", sha256=sha, size=7, commit_id="f" * 64
    )
    entry = [e for e in await _ours(owner_client) if e["oid"] == sha][0]
    assert entry["ref"] is None and entry["filename"] == "ckpt/old.bin" and entry["size"] == 7


# ----- the deletion is not supported -----


@pytest.mark.parametrize(
    "body",
    [
        {"deletions": {"sha": ["a" * 64], "rewriteHistory": False}},
        {"deletions": {"sha": ["a" * 64], "rewriteHistory": True}},
        {"deletions": {"sha": []}},
        {"deletions": {"sha": ["not a sha"]}},
        {},
        {"anything": [1, 2]},
    ],
)
async def test_the_batch_is_not_supported_whatever_it_asks(m, owner_client, body):
    response = await owner_client.post(BATCH, json=body)
    assert response.status_code == 501
    assert response.headers["x-error-code"] == "NotImplemented"
    message = response.headers["x-error-message"]
    assert "permanently_delete_lfs_files is not supported" in message and "LFS keep versions" in message


async def test_nothing_is_deleted_by_the_batch(m, owner_client):
    _, oids = await _versions(m, owner_client, 3)
    _age_recent(m)
    response = await owner_client.post(BATCH, json={"deletions": {"sha": oids, "rewriteHistory": False}})
    assert response.status_code == 501
    assert all(_stored(m, oid) for oid in oids) and all(m.gc.tombstone_state(oid) is None for oid in oids)
    assert [e["oid"] for e in await _ours(owner_client)] == oids[::-1]


async def test_the_batch_still_asks_who_you_are_and_what_you_may_do(m, owner_client, member_client, visitor_client):
    assert (await visitor_client.post(BATCH, json={})).status_code in (401, 403, 404)
    assert (await member_client.post(BATCH, json={})).status_code in (403, 404)  # not the repository's owner
    assert (await owner_client.post("/api/models/owner/nope/lfs-files/batch", json={})).status_code == 404
