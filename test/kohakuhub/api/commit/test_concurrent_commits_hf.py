"""The race of #56 and the stale parent of #57, driven by real ``huggingface_hub`` clients.

Threads call ``HfApi.upload_file`` / ``create_commit`` against a live server, so the whole client
protocol runs (preupload, the LFS batch and verify calls, the commit). The invariants are the
ones a user cares about: a file whose upload was answered ``ok`` is in the repository, intact; a
refused upload left nothing behind; every commit holds exactly the file of the request that made
it. Real LakeFS, database and bucket.
"""

import asyncio
import hashlib
import time

import pytest
from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
from huggingface_hub.utils import HfHubHTTPError

from test.kohakuhub.api.commit.test_availability import _file, _live
from test.kohakuhub.api.helpers import encode_ndjson
from test.kohakuhub.support.db import concurrent_writers_need_postgres
from test.kohakuhub.api.commit.test_concurrent_commits import (  # noqa: F401 - m is the fixture
    _added_by,
    _collect_a_day_later,
    _commits,
    _file_paths,
    _in_storage,
    _new,
    _staged,
    _tree,
    m,
)

pytestmark = pytest.mark.hf_client


def _upload(api, repo_id, i, content):
    """One upload; (index, status, Retry-After of a refusal)."""
    try:
        api.upload_file(
            path_or_fileobj=content,
            path_in_repo=f"par/{i}.bin" if len(content) > 100 else f"par/{i}.txt",
            repo_id=repo_id,
            commit_message=f"upload {i}",
        )
        return i, 200, None
    except HfHubHTTPError as error:
        return i, error.response.status_code, error.response.headers.get("retry-after")


def _contents(count):
    # every other file is big enough to be an LFS file (and takes the LFS upload protocol)
    return {i: (f"lfs content {i} ".encode() * 40 if i % 2 else f"c{i}".encode()) for i in range(count)}


def _path(i, content):
    return f"par/{i}.bin" if len(content) > 100 else f"par/{i}.txt"


@concurrent_writers_need_postgres
async def test_parallel_uploads_to_one_branch(
    m, owner_client, live_server_url, hf_api_token, tmp_path
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    repo = await _new(m, owner_client, "hf-serial")
    initial = await repo.head()
    contents = _contents(12)

    results = await asyncio.gather(
        *(asyncio.to_thread(_upload, api, repo.id, i, contents[i]) for i in contents)
    )

    assert {status for _, status, _ in results} <= {200, 429}, results  # never a 500
    winners = {i for i, status, _ in results if status == 200}
    assert winners
    assert all(retry == "2" for _, status, retry in results if status == 429)
    # the repository holds the winners' files and nothing else, none of it staged
    expected = {_path(i, contents[i]) for i in winners}
    assert await _tree(m, repo) == expected
    assert _file_paths(m, repo) == expected
    assert await _staged(m, repo) == []
    # every commit carries exactly the file of the upload it answered
    commits = [c for c in await _commits(m, repo) if c["id"] != initial]
    assert sorted(c["message"] for c in commits) == sorted(f"upload {i}" for i in winners)
    for commit in commits:
        i = int(commit["message"].split()[1])
        assert await _added_by(m, repo, commit) == {_path(i, contents[i])}
    # and the bytes are the bytes that were uploaded
    for i in winners:
        downloaded = await asyncio.to_thread(
            hf_hub_download,
            repo.id,
            _path(i, contents[i]),
            endpoint=live_server_url,
            token=hf_api_token,
            cache_dir=tmp_path,
        )
        assert open(downloaded, "rb").read() == contents[i]
    await _collect_a_day_later(m)
    for i in winners:
        if len(contents[i]) > 100:
            assert _in_storage(m, hashlib.sha256(contents[i]).hexdigest())


@concurrent_writers_need_postgres
async def test_parallel_uploads_that_try_again_all_land(
    m, owner_client, live_server_url, hf_api_token
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    repo = await _new(m, owner_client, "hf-serial-retry")
    initial = await repo.head()
    contents = _contents(8)

    def until_it_lands(i):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            _, status, _ = _upload(api, repo.id, i, contents[i])
            if status == 200:
                return True
            assert status in (412, 429), status  # what the Hub answers; the documented retry is on these
            time.sleep(0.05)
        return False

    landed = await asyncio.gather(*(asyncio.to_thread(until_it_lands, i) for i in contents))

    assert all(landed)
    expected = {_path(i, contents[i]) for i in contents}
    assert await _tree(m, repo) == expected
    assert _file_paths(m, repo) == expected
    commits = [c for c in await _commits(m, repo) if c["id"] != initial]
    assert len(commits) == len(contents)  # one commit each, none swept into another's
    for commit in commits:
        i = int(commit["message"].split()[1])
        assert await _added_by(m, repo, commit) == {_path(i, contents[i])}
    await _collect_a_day_later(m)
    for i in contents:
        if len(contents[i]) > 100:
            assert _in_storage(m, hashlib.sha256(contents[i]).hexdigest())


async def test_create_commit_on_a_stale_parent_commit(
    m, owner_client, live_server_url, hf_api_token
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    repo = await _new(m, owner_client, "hf-parent")

    def info_sha():
        return api.repo_info(repo.id).sha

    def commit(path, parent=None):
        return api.create_commit(
            repo.id,
            [CommitOperationAdd(path, path.encode())],
            commit_message=f"add {path}",
            parent_commit=parent,
        )

    await asyncio.to_thread(commit, "first.txt")
    stale = await asyncio.to_thread(info_sha)  # a repository with no commit of its own has no sha
    assert stale
    await asyncio.to_thread(commit, "second.txt")

    with pytest.raises(HfHubHTTPError) as refused:
        await asyncio.to_thread(commit, "third.txt", stale)
    assert refused.value.response.status_code == 412
    assert "A commit has happened since" in (refused.value.server_message or "")  # the user sees why
    assert await _tree(m, repo) == {"first.txt", "second.txt"}
    assert await _staged(m, repo) == []

    current = await asyncio.to_thread(info_sha)
    info = await asyncio.to_thread(commit, "third.txt", current)
    assert info.commit_message == "add third.txt"
    assert await _tree(m, repo) == {"first.txt", "second.txt", "third.txt"}


async def test_create_commit_on_a_busy_branch_says_why(m, owner_client, live_server_url, hf_api_token):
    """A commit that waited too long in the queue is the Hub's 429, with the Hub's words, and the
    huggingface_hub user is shown them."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    repo = await _new(m, owner_client, "hf-busy")
    row_id = m.db.Repository.get(m.db.Repository.full_id == repo.id).id
    token = m.lock.acquire(row_id, "main")
    try:
        with pytest.raises(HfHubHTTPError) as refused:
            await asyncio.to_thread(
                api.create_commit, repo.id, [CommitOperationAdd("a.txt", b"a")], commit_message="a"
            )
    finally:
        m.lock.release(row_id, "main", token)
    assert refused.value.response.status_code == 429
    assert refused.value.response.headers.get("retry-after") == "2"
    assert "maximum time in concurrency queue reached" in (refused.value.server_message or "")


@concurrent_writers_need_postgres
async def test_parallel_upload_file_calls_all_succeed_like_on_the_hub(
    m, owner_client, live_server_url, hf_api_token, monkeypatch
):
    """The plain huggingface_hub call, no retry of ours: concurrent commits to one branch queue up
    and every call returns, as against the Hub."""
    monkeypatch.setattr(m.lock, "QUEUE_SECONDS", 120)
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    repo = await _new(m, owner_client, "hf-queue")
    contents = _contents(12)

    results = await asyncio.gather(
        *(asyncio.to_thread(_upload, api, repo.id, i, contents[i]) for i in contents)
    )

    assert [status for _, status, _ in results] == [200] * 12, results
    expected = {_path(i, contents[i]) for i in contents}
    assert await _tree(m, repo) == expected and _file_paths(m, repo) == expected
    assert await _staged(m, repo) == []


async def test_http_backoff_rides_out_a_busy_branch(m, owner_client, live_server_url, hf_api_token):
    """What huggingface_hub does with a retryable status: its own `http_backoff`, which its upload
    pipeline sends commits with (that pipeline is not reachable here, the suite keeps the client off
    Xet and it is used only with Xet). A commit refused with 429 is sent again after the wait and goes
    through once the branch is free."""
    from huggingface_hub.utils import http_backoff

    repo = await _new(m, owner_client, "hf-backoff")
    # the client the test's loop just made must not be reused by the live server's loop (see test_super_squash)
    _live("kohakuhub.lakefs_rest_client")._singleton_client = None
    row_id = m.db.Repository.get(m.db.Repository.full_id == repo.id).id
    payload = encode_ndjson(
        [{"key": "header", "value": {"summary": "via backoff"}}, _file("a.txt", "a")]
    )

    def commit():
        return http_backoff(
            "POST",
            f"{live_server_url}/api/models/{repo.id}/commit/main",
            data=payload,
            headers={"Authorization": f"Bearer {hf_api_token}", "Content-Type": "application/x-ndjson"},
            retry_on_status_codes=(429, 503),
            base_wait_time=0.2,
        )

    token = m.lock.acquire(row_id, "main")
    sending = asyncio.create_task(asyncio.to_thread(commit))
    await asyncio.sleep(1.0)  # refused (429) at least once while the branch is held
    assert not sending.done()
    m.lock.release(row_id, "main", token)
    response = await asyncio.wait_for(sending, timeout=60)

    assert response.status_code == 200, response.text
    assert await _tree(m, repo) == {"a.txt"}
