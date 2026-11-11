"""The times the API sends (revision, resolve headers) and the rows a commit writes are UTC, whatever the zone of the server process.

LakeFS reports epoch seconds; turning them into a time with the zone of the server process and a literal Z in front put the server's
local time on the wire (-8 h on a server in Los Angeles, +8 h in Shanghai). The rows the application writes are timezone-aware UTC
by construction; these tests keep both true when the process runs in any zone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import kohakuhub.api.files as files_api
from kohakuhub.db import Commit, File, Repository, User
from kohakuhub.db_operations import create_commit
from test.kohakuhub.support.factories import make_user
from test.kohakuhub.support.zones import ZONES, in_zone

pytestmark = pytest.mark.usefixtures("db_scope")

EPOCH = 1700000000  # 2023-11-14T22:13:20Z


def _http_error(status):
    request = httpx.Request("GET", "http://lakefs/x")
    return httpx.HTTPStatusError(
        f"{status} from LakeFS", request=request, response=httpx.Response(status, request=request)
    )


class _Lakefs:
    """LakeFS stand-in: one branch, one object, the commit is unknown (a plain branch name)."""

    def __init__(self):
        self.stat = {
            "physical_address": "s3://bucket/key/path.txt",
            "size_bytes": 5,
            "content_type": "text/plain",
            "mtime": EPOCH,
        }

    async def get_branch(self, **kwargs):
        return {"commit_id": "commit-sha"}

    async def get_commit(self, **kwargs):
        raise _http_error(404)

    async def stat_object(self, **kwargs):
        return self.stat


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


@pytest.fixture
def served(monkeypatch):
    """The file routes with the repository row real and LakeFS, S3 and the permission check stood in."""
    owner = make_user("alice")
    repo = Repository.create(repo_type="model", namespace="alice", name="demo", full_id="alice/demo", owner=owner)
    monkeypatch.setattr(files_api, "get_repository", lambda repo_type, namespace, name: repo)
    monkeypatch.setattr(files_api, "check_repo_read_permission", lambda repo_row, user: None)
    monkeypatch.setattr(files_api, "resolve_lakefs_repo", lambda repo_row: "lake")
    monkeypatch.setattr(files_api, "get_lakefs_client", lambda: _Lakefs())
    monkeypatch.setattr(files_api, "parse_s3_uri", lambda uri: ("bucket", "key"))
    monkeypatch.setattr(
        files_api, "generate_download_presigned_url", _async_return("https://download.example.com/file")
    )
    monkeypatch.setattr(
        files_api, "get_file", lambda repo_row, path, branch="main": SimpleNamespace(sha256="sha", lfs=False)
    )
    monkeypatch.setattr(files_api, "hf_siblings_json", _async_return("[]"))
    monkeypatch.setattr(files_api, "resolve_revision", _async_return(("commit-1", {"creation_date": EPOCH})))
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ZONES)
async def test_the_revision_time_is_utc_in_any_server_zone(served, zone):
    with in_zone(zone):
        response = await files_api.get_revision.__wrapped__(
            files_api.RepoType.model, "alice", "demo", "main", request=None, user=None, expand=None, blobs=False
        )
    assert json.loads(response.body)["lastModified"] == "2023-11-14T22:13:20.000000Z"


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ZONES)
async def test_the_last_modified_header_of_resolve_is_gmt_in_any_server_zone(served, zone):
    with in_zone(zone):
        _url, headers = await files_api._get_file_metadata("model", "alice", "demo", "main", "path.txt", None)
    assert headers["Last-Modified"] == "Tue, 14 Nov 2023 22:13:20 GMT"


@pytest.mark.asyncio
async def test_an_object_without_a_time_has_an_empty_last_modified_header(served, monkeypatch):
    client = _Lakefs()
    client.stat = {**client.stat, "mtime": 0}
    monkeypatch.setattr(files_api, "get_lakefs_client", lambda: client)
    _url, headers = await files_api._get_file_metadata("model", "alice", "demo", "main", "path.txt", None)
    assert headers["Last-Modified"] == ""


@pytest.mark.parametrize("zone", ZONES)
def test_rows_a_commit_writes_are_utc_in_any_server_zone(zone):
    """Defaults, ``create_commit`` and the file rows read the clock as UTC: the process zone never reaches the database."""
    with in_zone(zone):
        owner = User.create(username="writer", normalized_name="writer", email="writer@example.com")
        repo = Repository.create(repo_type="model", namespace="writer", name="demo", full_id="writer/demo", owner=owner)
        create_commit(
            commit_id="c" * 40, repository=repo, repo_type="model", branch="dev",
            author=owner, username="writer", message="a commit",
        )
        file = File.create(
            repository=repo, owner_id=owner.id, path_in_repo="a.txt", sha256="s", size=1, lfs=False, branch="dev"
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, as the columns hold it
        written = {
            "user.created_at": User.get_by_id(owner.id).created_at,
            "repository.created_at": Repository.get_by_id(repo.id).created_at,
            "commit.created_at": Commit.get(Commit.commit_id == "c" * 40).created_at,
            "file.created_at": File.get_by_id(file.id).created_at,
            "file.updated_at": File.get_by_id(file.id).updated_at,
        }
    for column, value in written.items():
        assert abs(value - now) < timedelta(seconds=30), f"{column} is {value}, UTC now is {now} (server zone {zone})"
