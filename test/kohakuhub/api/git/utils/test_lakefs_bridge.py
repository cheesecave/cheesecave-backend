"""Tests for the pure Python LakeFS bridge helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kohakuhub.api.git.utils.lakefs_bridge as lakefs_bridge
from kohakuhub.api.git.utils.objects import create_blob_object
from kohakuhub.utils.lakefs import lakefs_repo_name, resolve_lakefs_repo
from test.kohakuhub.support.factories import make_file, make_repo, make_user
from test.kohakuhub.support.fakes import FakeLakeFSClient, FakeS3Service


def _make_bridge(monkeypatch, client):
    monkeypatch.setattr(lakefs_bridge, "get_lakefs_client", lambda: client)
    return lakefs_bridge.GitLakeFSBridge("model", "owner", "demo", lakefs_repo="m-owner-demo")


def test_create_lfs_pointer_and_lfsconfig_use_hf_compatible_format():
    pointer = lakefs_bridge.create_lfs_pointer("abc123", 42).decode("utf-8")
    config = lakefs_bridge.generate_lfsconfig("https://hub.local", "owner", "demo").decode("utf-8")

    assert "version https://git-lfs.github.com/spec/v1" in pointer
    assert "oid sha256:abc123" in pointer
    assert "size 42" in pointer
    assert "\turl = https://hub.local/owner/demo.git/info/lfs" in config


@pytest.mark.asyncio
async def test_get_refs_returns_head_and_branch_sha(monkeypatch):
    client = SimpleNamespace(get_branch=None)
    bridge = _make_bridge(monkeypatch, client)

    async def fake_get_branch(repository: str, branch: str):
        return {"commit_id": "commit-1"}

    async def fake_build_commit_sha1(branch: str, commit_id: str):
        assert branch == "main"
        assert commit_id == "commit-1"
        return "a" * 40

    client.get_branch = fake_get_branch
    monkeypatch.setattr(bridge, "_build_commit_sha1", fake_build_commit_sha1)

    assert await bridge.get_refs() == {
        "refs/heads/main": "a" * 40,
        "HEAD": "a" * 40,
    }


@pytest.mark.asyncio
async def test_get_refs_returns_empty_on_missing_commit_or_errors(monkeypatch):
    async def missing_branch(repository: str, branch: str):
        return {}

    async def broken_branch(repository: str, branch: str):
        raise RuntimeError("boom")

    bridge = _make_bridge(monkeypatch, SimpleNamespace(get_branch=missing_branch))
    assert await bridge.get_refs() == {}

    bridge = _make_bridge(monkeypatch, SimpleNamespace(get_branch=broken_branch))
    assert await bridge.get_refs() == {}


def test_parse_gitattributes_match_patterns_and_generate_output(monkeypatch):
    bridge = _make_bridge(monkeypatch, SimpleNamespace())
    patterns = bridge._parse_gitattributes(
        """
        # comment
        *.bin filter=lfs diff=lfs merge=lfs -text
        models/*.safetensors filter=lfs diff=lfs merge=lfs -text
        """
    )

    assert patterns == {"*.bin", "models/*.safetensors"}
    assert bridge._matches_pattern("weights/model.bin", patterns) is True
    assert bridge._matches_pattern("models/model.safetensors", patterns) is True
    assert bridge._matches_pattern("docs/readme.md", patterns) is False
    generated = bridge._generate_gitattributes(["weights.bin", "models/model.safetensors"]).decode("utf-8")
    assert generated.splitlines()[0] == "# Git LFS tracking (auto-generated)"
    assert "models/model.safetensors filter=lfs diff=lfs merge=lfs -text" in generated


@pytest.mark.asyncio
async def test_build_blob_sha1s_creates_regular_blobs_lfs_pointers_and_support_files(monkeypatch, db_scope):
    s3 = FakeS3Service()
    client = FakeLakeFSClient(s3_service=s3, default_bucket="bucket")
    await client.create_repository("m-owner-demo", "s3://bucket/m-owner-demo", default_branch="main")
    await client.upload_object("m-owner-demo", "main", "README.md", b"readme")
    await client.upload_object("m-owner-demo", "main", "weights/model.safetensors", b"large-binary")
    await client.commit("m-owner-demo", "main", "seed objects")
    bridge = _make_bridge(monkeypatch, client)

    repo = make_repo(make_user("owner"), "demo")
    make_file(repo, "weights/model.safetensors", "f" * 64, size=12, lfs=True)
    monkeypatch.setattr(lakefs_bridge, "should_use_lfs", lambda repo, path, size: False)
    monkeypatch.setattr(lakefs_bridge.cfg.app, "base_url", "https://hub.local")

    blob_data = await bridge._build_blob_sha1s(
        [
            {"path": "README.md", "path_type": "object", "size_bytes": 6},
            {"path": "weights/model.safetensors", "path_type": "object", "size_bytes": 12},
        ],
        branch="main",
    )

    readme_blob = blob_data["README.md"][1].split(b"\0", 1)[1]
    lfs_pointer = blob_data["weights/model.safetensors"][1].split(b"\0", 1)[1].decode("utf-8")
    gitattributes = blob_data[".gitattributes"][1].split(b"\0", 1)[1].decode("utf-8")
    lfsconfig = blob_data[".lfsconfig"][1].split(b"\0", 1)[1].decode("utf-8")

    assert readme_blob == b"readme"
    assert "oid sha256:" + "f" * 64 in lfs_pointer
    assert "weights/model.safetensors filter=lfs" in gitattributes
    assert "https://hub.local/owner/demo.git/info/lfs" in lfsconfig


@pytest.mark.asyncio
async def test_build_blob_sha1s_reads_the_rows_of_the_branch_served(monkeypatch, db_scope):
    """A side branch's LFS pointer is the one its own row names, not main's (#11, stage 3)."""
    s3 = FakeS3Service()
    client = FakeLakeFSClient(s3_service=s3, default_bucket="bucket")
    await client.create_repository("m-owner-demo", "s3://bucket/m-owner-demo", default_branch="main")
    await client.upload_object("m-owner-demo", "main", "weights/model.safetensors", b"large-binary")
    await client.commit("m-owner-demo", "main", "seed objects")
    await client.create_branch("m-owner-demo", "dev", source="main")
    bridge = _make_bridge(monkeypatch, client)

    repo = make_repo(make_user("owner"), "demo")
    make_file(repo, "weights/model.safetensors", "f" * 64, size=12, lfs=True)
    make_file(repo, "weights/model.safetensors", "e" * 64, size=12, lfs=True, branch="dev")
    monkeypatch.setattr(lakefs_bridge, "should_use_lfs", lambda repo, path, size: False)
    monkeypatch.setattr(lakefs_bridge.cfg.app, "base_url", "https://hub.local")

    objects = [{"path": "weights/model.safetensors", "path_type": "object", "size_bytes": 12}]
    on_dev = await bridge._build_blob_sha1s(objects, branch="dev")
    on_main = await bridge._build_blob_sha1s(objects, branch="main")

    assert "oid sha256:" + "e" * 64 in on_dev["weights/model.safetensors"][1].decode("latin-1")
    assert "oid sha256:" + "f" * 64 in on_main["weights/model.safetensors"][1].decode("latin-1")


@pytest.mark.asyncio
async def test_build_blob_sha1s_respects_existing_gitattributes(monkeypatch, db_scope):
    s3 = FakeS3Service()
    client = FakeLakeFSClient(s3_service=s3, default_bucket="bucket")
    await client.create_repository("m-owner-demo", "s3://bucket/m-owner-demo", default_branch="main")
    await client.upload_object(
        "m-owner-demo",
        "main",
        ".gitattributes",
        b"*.bin filter=lfs diff=lfs merge=lfs -text\n",
    )
    await client.upload_object("m-owner-demo", "main", "artifacts/model.bin", b"binary")
    await client.commit("m-owner-demo", "main", "seed objects")
    bridge = _make_bridge(monkeypatch, client)

    make_repo(make_user("owner"), "demo")  # a repository with no File rows yet
    monkeypatch.setattr(lakefs_bridge, "should_use_lfs", lambda repo, path, size: False)
    monkeypatch.setattr(lakefs_bridge.cfg.app, "base_url", "https://hub.local")

    blob_data = await bridge._build_blob_sha1s(
        [
            {"path": ".gitattributes", "path_type": "object", "size_bytes": 42},
            {"path": "artifacts/model.bin", "path_type": "object", "size_bytes": 6},
        ],
        branch="main",
    )

    assert ".gitattributes" in blob_data
    pointer = blob_data["artifacts/model.bin"][1].split(b"\0", 1)[1].decode("utf-8")
    assert "oid sha256:" in pointer


@pytest.mark.asyncio
async def test_build_commit_sha1_and_pack_file_cover_success_and_empty_paths(monkeypatch):
    class FakeBridgeClient:
        async def list_objects(self, repository, ref, prefix="", after="", amount=1000):
            return {
                "results": [{"path": "README.md", "path_type": "object"}],
                "pagination": {"has_more": False},
            }

        async def get_commit(self, repository, commit_id):
            return {
                "committer": "Alice",
                "message": "Snapshot",
                "creation_date": "2025-01-01T00:00:00Z",
            }

        async def get_branch(self, repository, branch):
            return {"commit_id": "commit-1"}

    client = FakeBridgeClient()
    bridge = _make_bridge(monkeypatch, client)
    blob_sha1, blob_with_header = create_blob_object(b"readme")

    async def fake_build_blob_sha1s(file_objects, branch):
        return {"README.md": (blob_sha1, blob_with_header, "100644")}

    monkeypatch.setattr(bridge, "_build_blob_sha1s", fake_build_blob_sha1s)

    commit_sha1 = await bridge._build_commit_sha1("main", "commit-1")
    pack_bytes = await bridge.build_pack_file([], [], branch="main")

    assert commit_sha1
    assert pack_bytes.startswith(b"PACK")

    class EmptyClient(FakeBridgeClient):
        async def list_objects(self, repository, ref, prefix="", after="", amount=1000):
            return {"results": [], "pagination": {"has_more": False}}

    empty_bridge = _make_bridge(monkeypatch, EmptyClient())
    assert await empty_bridge._build_commit_sha1("main", "commit-1") is None
    assert (await empty_bridge.build_pack_file([], [], branch="main")).startswith(b"PACK")


def test_bridge_requires_the_lakefs_repo_it_talks_to(monkeypatch):
    monkeypatch.setattr(lakefs_bridge, "get_lakefs_client", lambda: None)

    with pytest.raises(TypeError):
        lakefs_bridge.GitLakeFSBridge("model", "owner", "demo")


@pytest.mark.parametrize("value", [None, ""])
def test_bridge_rejects_an_empty_lakefs_repo_instead_of_deriving_one(monkeypatch, value):
    monkeypatch.setattr(lakefs_bridge, "get_lakefs_client", lambda: None)

    with pytest.raises(ValueError):
        lakefs_bridge.GitLakeFSBridge("model", "owner", "demo", lakefs_repo=value)


def test_bridge_uses_the_stored_id_of_a_row_resolved_from_the_database(monkeypatch, db_scope):
    monkeypatch.setattr(lakefs_bridge, "get_lakefs_client", lambda: None)
    stored = make_repo(make_user("owner"), "demo", lakefs_repo="m-stored-demo-x7k2")

    bridge = lakefs_bridge.GitLakeFSBridge(
        "model", "owner", "demo", lakefs_repo=resolve_lakefs_repo(stored)
    )

    assert bridge.lakefs_repo == "m-stored-demo-x7k2"


def test_bridge_derives_the_name_for_a_legacy_row_without_a_stored_id(monkeypatch, db_scope):
    monkeypatch.setattr(lakefs_bridge, "get_lakefs_client", lambda: None)
    legacy = make_repo(make_user("owner"), "demo")
    assert legacy.lakefs_repo is None

    bridge = lakefs_bridge.GitLakeFSBridge(
        "model", "owner", "demo", lakefs_repo=resolve_lakefs_repo(legacy)
    )

    assert bridge.lakefs_repo == lakefs_repo_name("model", "owner/demo")
