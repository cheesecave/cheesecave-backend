"""`list_lfs_files` and `permanently_delete_lfs_files`: a repository's LFS objects, and freeing their storage (#66).

`GET /api/{type}s/{repo}/lfs-files` lists what a repository stores as LFS; `POST .../lfs-files/batch` deletes objects for
good. Real database and bucket (the shared `m` fixture of `test_lfs_gc.py`): the decision whether an object may go is the
garbage collection's own (`lfs_gc`), so an object another repository or a branch head still links is refused.
"""

import hashlib

import pytest
from botocore.exceptions import ClientError

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


def _body(*shas, rewrite=False):
    return {"deletions": {"sha": list(shas), "rewriteHistory": rewrite}}


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
    assert (await owner_client.post("/api/models/owner/nope/lfs-files/batch", json=_body("a" * 64))).status_code == 404


# ----- deleting -----


async def test_an_old_version_is_deleted_for_good_and_the_branch_head_is_not(m, owner_client):
    contents, oids = await _versions(m, owner_client, 3)
    _age_recent(m)
    used_before = m.db.Repository.get_by_id(_repo(m).id).lfs_bytes

    response = await owner_client.post(BATCH, json=_body(oids[0], oids[1]))
    assert response.status_code == 200, response.text
    assert sorted(response.json()["deleted"]) == sorted([oids[0], oids[1]])

    assert [_stored(m, oid) for oid in oids] == [False, False, True]
    assert m.gc.tombstone_state(oids[0]) == m.gc.DELETED
    assert [e["oid"] for e in await _ours(owner_client)] == [oids[2]]
    # history rows stay (what says a commit lost its content); the quota is what the tombstones say is gone
    H = m.db.LFSObjectHistory
    assert H.select().where((H.repository == _repo(m)) & (H.sha256 == oids[0])).exists()
    freed = used_before - m.db.Repository.get_by_id(_repo(m).id).lfs_bytes
    assert freed == len(contents[0]) + len(contents[1])


async def test_what_a_branch_head_links_is_refused_and_nothing_is_deleted(m, owner_client):
    _, oids = await _versions(m, owner_client, 3)
    _age_recent(m)
    response = await owner_client.post(BATCH, json=_body(oids[0], oids[2]))  # oids[2] is main's file
    assert response.status_code == 409
    in_use = response.json()["inUse"]
    assert in_use == {oids[2]: "head"}  # why, per object
    assert "nothing was deleted" in response.headers["x-error-message"].lower()
    assert all(_stored(m, oid) for oid in oids)
    assert m.gc.tombstone_state(oids[0]) is None


async def test_an_object_another_repository_relies_on_is_refused(m, owner_client):
    contents, oids = await _versions(m, owner_client, 2)
    other = _repo(m, "acme-labs/private-dataset", "dataset")
    m.db.LfsHeadRef.create(repository=other, branch="main", path_in_repo="x/shared.bin", sha256=oids[0])
    _age_recent(m)
    response = await owner_client.post(BATCH, json=_body(oids[0]))
    assert response.status_code == 409 and response.json()["inUse"] == {oids[0]: "head"}
    assert _stored(m, oids[0])


async def test_an_upload_still_in_its_grace_period_is_refused(m, owner_client):
    _, oids = await _versions(m, owner_client, 2)
    response = await owner_client.post(BATCH, json=_body(oids[0]))  # not aged: a commit just claimed it
    assert response.status_code == 409 and response.json()["inUse"] == {oids[0]: "recent"}
    _age_recent(m)
    assert (await owner_client.post(BATCH, json=_body(oids[0]))).status_code == 200


async def test_rewriting_history_is_refused_and_nothing_is_deleted(m, owner_client):
    _, oids = await _versions(m, owner_client, 2)
    _age_recent(m)
    response = await owner_client.post(BATCH, json=_body(oids[0], rewrite=True))
    assert response.status_code == 501
    assert "rewrite_history=False" in response.headers["x-error-message"]
    assert _stored(m, oids[0])


async def test_an_object_that_is_not_the_repositorys_is_not_found(m, owner_client):
    _, oids = await _versions(m, owner_client, 2)
    _age_recent(m)
    stranger = _sha("nobody stores this")
    response = await owner_client.post(BATCH, json=_body(oids[0], stranger))
    assert response.status_code == 404 and response.json()["unknown"] == [stranger]
    assert _stored(m, oids[0])  # all or nothing


async def test_deleting_again_succeeds_and_says_so(m, owner_client):
    _, oids = await _versions(m, owner_client, 2)
    _age_recent(m)
    assert (await owner_client.post(BATCH, json=_body(oids[0]))).status_code == 200
    again = await owner_client.post(BATCH, json=_body(oids[0]))
    assert again.status_code == 200
    assert again.json() == {"deleted": [], "alreadyDeleted": [oids[0]], "kept": {}}


@pytest.mark.parametrize("sha", ["xyz", "a" * 63, "g" * 64, "", None, 7])
async def test_a_malformed_object_id_is_a_bad_request(m, owner_client, sha):
    response = await owner_client.post(BATCH, json=_body(sha))
    assert response.status_code in (400, 422)


async def test_an_empty_batch_and_a_malformed_body(m, owner_client):
    assert (await owner_client.post(BATCH, json=_body())).status_code == 200
    assert (await owner_client.post(BATCH, json={})).status_code == 422
    assert (await owner_client.post(BATCH, json={"deletions": {"sha": "a" * 64}})).status_code == 422
    too_many = await owner_client.post(BATCH, json=_body(*[f"{i:064x}" for i in range(1001)]))
    assert too_many.status_code == 422


async def test_ids_are_case_insensitive_and_repeated_ids_count_once(m, owner_client):
    _, oids = await _versions(m, owner_client, 2)
    _age_recent(m)
    response = await owner_client.post(BATCH, json=_body(oids[0].upper(), oids[0]))
    assert response.status_code == 200 and response.json()["deleted"] == [oids[0]]


async def test_only_who_may_delete_the_repository_may_delete_its_objects(m, owner_client, member_client, visitor_client):
    _, oids = await _versions(m, owner_client, 2)
    _age_recent(m)
    assert (await visitor_client.post(BATCH, json=_body(oids[0]))).status_code in (403, 404)
    assert (await member_client.post(BATCH, json=_body(oids[0]))).status_code in (403, 404)
    assert _stored(m, oids[0])


async def test_the_bytes_a_claim_holds_for_the_repository_go_too(m, owner_client):
    contents, oids = await _versions(m, owner_client, 2)
    repo = _repo(m)
    m.db.LfsGrant.update(kind="claim", proven=True).where(
        (m.db.LfsGrant.repository == repo) & (m.db.LfsGrant.sha256 == oids[0])
    ).execute()
    key = m.gc.claim_key(repo.id, oids[0])
    m.s3.put_object(Bucket=m.cfg.s3.bucket, Key=key, Body=contents[0])
    _age_recent(m)
    assert (await owner_client.post(BATCH, json=_body(oids[0]))).status_code == 200
    with pytest.raises(ClientError):
        m.s3.head_object(Bucket=m.cfg.s3.bucket, Key=key)
    assert not m.db.LfsGrant.select().where(
        (m.db.LfsGrant.repository == repo) & (m.db.LfsGrant.sha256 == oids[0]) & (m.db.LfsGrant.kind == "claim")
    ).exists()


async def test_an_object_relied_on_in_the_meantime_is_kept_not_deleted(m, owner_client, monkeypatch):
    """The decision is taken again under the object's lock; a failing precheck cannot be provoked from outside.

    Targeted mock: another request relying on the object between the check and the delete is a race.
    """
    _, oids = await _versions(m, owner_client, 3)
    _age_recent(m)
    real = m.gc.begin_delete

    def raced(sha, **kwargs):
        return False if sha == oids[0] else real(sha, **kwargs)

    monkeypatch.setattr(m.gc, "begin_delete", raced)
    response = await owner_client.post(BATCH, json=_body(oids[0], oids[1]))
    assert response.status_code == 200
    assert response.json()["deleted"] == [oids[1]] and response.json()["kept"] == {oids[0]: "changed"}
    assert _stored(m, oids[0]) and not _stored(m, oids[1])


async def test_a_failure_of_storage_leaves_the_objects_as_they_were(m, owner_client, monkeypatch):
    """Targeted mock: an outage of the bucket cannot be produced against the real MinIO."""
    _, oids = await _versions(m, owner_client, 2)
    _age_recent(m)
    used = m.db.Repository.get_by_id(_repo(m).id).lfs_bytes

    def down(bucket, keys):
        raise RuntimeError("the bucket is unreachable")

    api = __import__("importlib").import_module("kohakuhub.api.lfs_files")
    monkeypatch.setattr(api, "_delete_keys", down)
    response = await owner_client.post(BATCH, json=_body(oids[0]))
    assert response.status_code == 500 and "unreachable" in response.headers["x-error-message"]
    assert m.gc.tombstone_state(oids[0]) is None  # not tombstoned: it is still stored
    assert _stored(m, oids[0])
    assert m.db.Repository.get_by_id(_repo(m).id).lfs_bytes == used  # and still counted
    assert [e["oid"] for e in await _ours(owner_client)] == oids[::-1]
    monkeypatch.undo()
    assert (await owner_client.post(BATCH, json=_body(oids[0]))).status_code == 200


async def test_a_version_no_file_or_commit_row_names_has_no_ref(m, owner_client):
    """History written before commits were recorded per branch names no ref."""
    sha = _sha("an old version")
    m.db.LFSObjectHistory.create(
        repository=_repo(m), path_in_repo="ckpt/old.bin", sha256=sha, size=7, commit_id="f" * 64
    )
    entry = [e for e in await _ours(owner_client) if e["oid"] == sha][0]
    assert entry["ref"] is None and entry["filename"] == "ckpt/old.bin" and entry["size"] == 7


def test_an_object_only_the_ignored_repository_relies_on_may_go_and_others_keep_it(m):
    repo, other = _repo(m), _repo(m, "acme-labs/private-dataset", "dataset")
    sha = _sha("history of two repositories")
    for owner in (repo, other):
        m.db.LFSObjectHistory.create(repository=owner, path_in_repo="ckpt/x.bin", sha256=sha, size=1, commit_id="c1")
    assert m.gc.retention_reason(sha) == "history"
    assert m.gc.retention_reason(sha, ignore_history_of=repo.id) == "history"  # the other still has it
    m.db.LFSObjectHistory.delete().where(m.db.LFSObjectHistory.repository == other).execute()
    assert m.gc.retention_reason(sha, ignore_history_of=repo.id) is None
    assert m.gc.retention_reason(sha) == "history"


def test_abort_delete_undoes_only_a_delete_that_has_not_finished(m):
    deleting, deleted, none = _sha("deleting"), _sha("deleted"), _sha("never tombstoned")
    m.db.LfsObjectTombstone.create(sha256=deleting, state=m.gc.DELETING)
    m.db.LfsObjectTombstone.create(sha256=deleted, state=m.gc.DELETED)
    m.gc.abort_delete([deleting, deleted, none])
    assert m.gc.tombstone_state(deleting) is None  # still stored: not gone
    assert m.gc.tombstone_state(deleted) == m.gc.DELETED  # finished: stays
    assert m.gc.tombstone_state(none) is None

