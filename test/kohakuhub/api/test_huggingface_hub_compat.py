"""Compatibility tests using the real huggingface_hub Python client."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from huggingface_hub import (
    CommitOperationAdd,
    CommitOperationDelete,
    HfApi,
    hf_hub_download,
    snapshot_download,
)


async def _create_hf_token(client, name: str) -> str:
    response = await client.post(
        "/api/auth/tokens/create",
        json={"name": name},
    )
    response.raise_for_status()
    return response.json()["token"]


def _set_repo_private(api: HfApi, repo_id: str, private: bool) -> None:
    update_settings = getattr(api, "update_repo_settings", None)
    if callable(update_settings):
        update_settings(repo_id, private=private)
        return

    update_visibility = getattr(api, "update_repo_visibility", None)
    if callable(update_visibility):
        update_visibility(repo_id, private=private)
        return

    pytest.skip("huggingface_hub does not expose repository visibility updates")


@pytest.fixture
async def member_hf_api_token(member_client):
    return await _create_hf_token(member_client, "hf-api-member")


@pytest.fixture
async def outsider_hf_api_token(outsider_client):
    return await _create_hf_token(outsider_client, "hf-api-outsider")


async def test_hf_api_repo_info_permissions_and_existence(
    live_server_url,
    hf_api_token,
    member_hf_api_token,
    outsider_hf_api_token,
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    member_api = HfApi(endpoint=live_server_url, token=member_hf_api_token)
    outsider_api = HfApi(endpoint=live_server_url, token=outsider_hf_api_token)

    whoami = await asyncio.to_thread(api.whoami)
    assert whoami["name"] == "owner"
    assert any(org["name"] == "acme-labs" for org in whoami["orgs"])

    info = await asyncio.to_thread(
        lambda: api.repo_info("owner/demo-model", files_metadata=True)
    )
    assert info.id == "owner/demo-model"
    assert any(sibling.rfilename == "README.md" for sibling in info.siblings)
    assert any(
        sibling.rfilename == "weights/model.safetensors" for sibling in info.siblings
    )

    assert await asyncio.to_thread(lambda: api.repo_exists("owner/demo-model")) is True
    assert (
        await asyncio.to_thread(
            lambda: api.file_exists("owner/demo-model", "README.md")
        )
        is True
    )
    assert (
        await asyncio.to_thread(
            lambda: api.file_exists("owner/demo-model", "missing.txt")
        )
        is False
    )

    private_dataset = await asyncio.to_thread(
        lambda: member_api.dataset_info(
            "acme-labs/private-dataset",
            files_metadata=True,
        )
    )
    assert private_dataset.private is True
    assert any(
        sibling.rfilename == "data/train.jsonl"
        for sibling in private_dataset.siblings
    )

    assert (
        await asyncio.to_thread(
            lambda: member_api.repo_exists(
                "acme-labs/private-dataset",
                repo_type="dataset",
            )
        )
        is True
    )
    assert (
        await asyncio.to_thread(
            lambda: member_api.file_exists(
                "acme-labs/private-dataset",
                "data/train.jsonl",
                repo_type="dataset",
            )
        )
        is True
    )
    assert (
        await asyncio.to_thread(
            lambda: outsider_api.repo_exists(
                "acme-labs/private-dataset",
                repo_type="dataset",
            )
        )
        is False
    )
    assert (
        await asyncio.to_thread(
            lambda: outsider_api.file_exists(
                "acme-labs/private-dataset",
                "data/train.jsonl",
                repo_type="dataset",
            )
        )
        is False
    )


async def test_hf_api_listings_tree_and_downloads(
    live_server_url,
    hf_api_token,
    member_hf_api_token,
    tmp_path,
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    member_api = HfApi(endpoint=live_server_url, token=member_hf_api_token)

    await asyncio.to_thread(
        lambda: api.create_repo(
            "owner/hf-space-listing",
            repo_type="space",
            space_sdk="static",
        )
    )

    models = await asyncio.to_thread(lambda: list(api.list_models(author="owner", limit=10)))
    datasets = await asyncio.to_thread(
        lambda: list(member_api.list_datasets(author="acme-labs", limit=10))
    )
    spaces = await asyncio.to_thread(lambda: list(api.list_spaces(author="owner", limit=10)))
    assert any(item.id == "owner/demo-model" for item in models)
    assert any(item.id == "acme-labs/private-dataset" for item in datasets)
    assert any(item.id == "owner/hf-space-listing" for item in spaces)

    space_info = await asyncio.to_thread(
        lambda: api.space_info("owner/hf-space-listing")
    )
    assert space_info.id == "owner/hf-space-listing"

    refs = await asyncio.to_thread(lambda: api.list_repo_refs("owner/demo-model"))
    assert any(branch.name == "main" for branch in refs.branches)

    tree_entries = await asyncio.to_thread(
        lambda: list(api.list_repo_tree("owner/demo-model", recursive=True))
    )
    paths = {entry.path for entry in tree_entries}
    assert "README.md" in paths
    assert "weights/model.safetensors" in paths

    downloaded = await asyncio.to_thread(
        lambda: hf_hub_download(
            repo_id="owner/demo-model",
            filename="README.md",
            endpoint=live_server_url,
            token=hf_api_token,
            cache_dir=tmp_path,
        )
    )
    assert Path(downloaded).read_text(encoding="utf-8") == "# Demo Model\n\nseed data\n"

    snapshot_dir = await asyncio.to_thread(
        lambda: snapshot_download(
            repo_id="owner/demo-model",
            endpoint=live_server_url,
            token=hf_api_token,
            cache_dir=tmp_path / "cache",
            local_dir=tmp_path / "snapshot",
        )
    )
    assert (Path(snapshot_dir) / "README.md").read_text(encoding="utf-8") == (
        "# Demo Model\n\nseed data\n"
    )
    assert (Path(snapshot_dir) / "weights" / "model.safetensors").read_bytes() == (
        b"safe tensor payload"
    )

async def test_hf_api_create_repo_upload_file_and_upload_folder(
    live_server_url,
    hf_api_token,
    tmp_path,
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    repo_url = await asyncio.to_thread(
        lambda: api.create_repo("owner/hf-api-created", exist_ok=False)
    )
    assert str(repo_url).endswith("/models/owner/hf-api-created")

    await asyncio.to_thread(
        lambda: api.upload_file(
            path_or_fileobj=b"hello from huggingface_hub\n",
            path_in_repo="nested/hf-api.txt",
            repo_id="owner/hf-api-created",
            commit_message="Upload through huggingface_hub",
        )
    )

    upload_folder = tmp_path / "upload-folder"
    (upload_folder / "docs").mkdir(parents=True)
    (upload_folder / "README.md").write_text("# Folder Upload\n", encoding="utf-8")
    (upload_folder / "docs" / "guide.md").write_text("hello\n", encoding="utf-8")

    await asyncio.to_thread(
        lambda: api.upload_folder(
            repo_id="owner/hf-api-created",
            folder_path=upload_folder,
            path_in_repo="bundle",
            commit_message="Upload folder through huggingface_hub",
        )
    )

    tree_entries = await asyncio.to_thread(
        lambda: list(api.list_repo_tree("owner/hf-api-created", recursive=True))
    )
    tree_paths = {entry.path for entry in tree_entries}
    assert "nested/hf-api.txt" in tree_paths
    assert "bundle/README.md" in tree_paths
    assert "bundle/docs/guide.md" in tree_paths


async def test_hf_api_create_commit_delete_file_and_list_commits(
    live_server_url,
    hf_api_token,
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    await asyncio.to_thread(lambda: api.create_repo("owner/hf-commit-compat"))
    await asyncio.to_thread(
        lambda: api.upload_file(
            path_or_fileobj=b"delete me\n",
            path_in_repo="delete-me.txt",
            repo_id="owner/hf-commit-compat",
            commit_message="Seed delete target",
        )
    )

    commit_info = await asyncio.to_thread(
        lambda: api.create_commit(
            "owner/hf-commit-compat",
            operations=[
                CommitOperationAdd(
                    path_in_repo="README.md",
                    path_or_fileobj=b"# Commit Compat\n",
                ),
                CommitOperationDelete(path_in_repo="delete-me.txt"),
            ],
            commit_message="Replace delete target",
        )
    )
    assert commit_info.oid

    commits = await asyncio.to_thread(
        lambda: api.list_repo_commits("owner/hf-commit-compat")
    )
    assert commits[0].title == "Replace delete target"
    assert commits[0].authors == ["owner"]
    assert any(commit.title == "Seed delete target" for commit in commits)

    assert (
        await asyncio.to_thread(
            lambda: api.file_exists("owner/hf-commit-compat", "README.md")
        )
        is True
    )
    assert (
        await asyncio.to_thread(
            lambda: api.file_exists("owner/hf-commit-compat", "delete-me.txt")
        )
        is False
    )

    await asyncio.to_thread(
        lambda: api.delete_file(
            "README.md",
            "owner/hf-commit-compat",
            commit_message="Remove README via huggingface_hub",
        )
    )
    assert (
        await asyncio.to_thread(
            lambda: api.file_exists("owner/hf-commit-compat", "README.md")
        )
        is False
    )


async def test_hf_api_branch_and_tag_lifecycle(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    await asyncio.to_thread(lambda: api.create_repo("owner/hf-refs-compat"))
    await asyncio.to_thread(
        lambda: api.create_branch("owner/hf-refs-compat", branch="feature-compat")
    )
    await asyncio.to_thread(
        lambda: api.create_tag("owner/hf-refs-compat", tag="v0.1.0")
    )

    refs = await asyncio.to_thread(lambda: api.list_repo_refs("owner/hf-refs-compat"))
    assert any(branch.name == "feature-compat" for branch in refs.branches)
    assert any(tag.name == "v0.1.0" for tag in refs.tags)

    await asyncio.to_thread(lambda: api.delete_tag("owner/hf-refs-compat", tag="v0.1.0"))
    await asyncio.to_thread(
        lambda: api.delete_branch("owner/hf-refs-compat", branch="feature-compat")
    )

    refs_after_delete = await asyncio.to_thread(
        lambda: api.list_repo_refs("owner/hf-refs-compat")
    )
    assert all(tag.name != "v0.1.0" for tag in refs_after_delete.tags)
    assert all(
        branch.name != "feature-compat" for branch in refs_after_delete.branches
    )


async def test_hf_api_likes_visibility_move_delete_and_list_liked_repos(
    live_server_url,
    hf_api_token,
    outsider_hf_api_token,
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    outsider_api = HfApi(endpoint=live_server_url, token=outsider_hf_api_token)

    await asyncio.to_thread(lambda: api.create_repo("owner/hf-lifecycle-compat"))

    likers = await asyncio.to_thread(
        lambda: list(api.list_repo_likers("owner/demo-model"))
    )
    assert [user.username for user in likers] == ["owner"]

    liked_repos = await asyncio.to_thread(lambda: api.list_liked_repos("owner"))
    assert "owner/demo-model" in liked_repos.models

    await asyncio.to_thread(lambda: api.unlike("owner/demo-model"))
    liked_after_unlike = await asyncio.to_thread(lambda: api.list_liked_repos("owner"))
    assert "owner/demo-model" not in liked_after_unlike.models

    like_method = getattr(api, "like", None)
    if callable(like_method):
        await asyncio.to_thread(lambda: like_method("owner/demo-model"))
        reliked = await asyncio.to_thread(lambda: api.list_liked_repos("owner"))
        assert "owner/demo-model" in reliked.models

    if callable(getattr(api, "update_repo_settings", None)) or callable(
        getattr(api, "update_repo_visibility", None)
    ):
        await asyncio.to_thread(
            lambda: _set_repo_private(api, "owner/hf-lifecycle-compat", True)
        )
        private_info = await asyncio.to_thread(
            lambda: api.repo_info("owner/hf-lifecycle-compat")
        )
        assert private_info.private is True
        assert (
            await asyncio.to_thread(
                lambda: outsider_api.repo_exists("owner/hf-lifecycle-compat")
            )
            is False
        )

    await asyncio.to_thread(
        lambda: api.move_repo(
            "owner/hf-lifecycle-compat",
            "owner/hf-lifecycle-renamed",
        )
    )
    assert (
        await asyncio.to_thread(
            lambda: api.repo_exists("owner/hf-lifecycle-compat")
        )
        is False
    )
    assert (
        await asyncio.to_thread(
            lambda: api.repo_exists("owner/hf-lifecycle-renamed")
        )
        is True
    )

    await asyncio.to_thread(lambda: api.delete_repo("owner/hf-lifecycle-renamed"))
    assert (
        await asyncio.to_thread(
            lambda: api.repo_exists("owner/hf-lifecycle-renamed")
        )
        is False
    )


# ---------------------------------------------------------------------------
# Issue #93: renaming a repo must free its name immediately.
# ---------------------------------------------------------------------------


def test_hf_client_still_retries_on_our_conflict_sentence():
    """Pin our retry sentence to the one huggingface_hub actually looks for.

    `HfApi.create_repo` wraps its POST in `while True` and retries only when the
    response body contains this exact sentence. If a future huggingface_hub
    reworded it, our 409 would stop being retryable and - worse - would start
    being swallowed by `create_repo(exist_ok=True)` as "already exists". This
    runs against every version in the CI matrix, so it fails loudly if that
    happens rather than silently degrading.
    """
    import inspect

    from huggingface_hub import hf_api as hf_api_module

    from kohakuhub.api.repo.routers.crud import LAKEFS_CONFLICT_RETRY_MESSAGE

    source = inspect.getsource(hf_api_module)
    assert LAKEFS_CONFLICT_RETRY_MESSAGE in source, (
        "huggingface_hub no longer contains the conflict sentence we emit; "
        "create_repo will not retry our recycling 409 anymore"
    )


async def test_hf_move_repo_frees_the_old_name_for_immediate_reuse(
    live_server_url,
    hf_api_token,
):
    """End-to-end reproduction of issue #93.

    Renaming a dataset and immediately recreating the original name used to fail
    with a 500 wrapping LakeFS's `409 not unique`, because the LakeFS repository
    id is derived from the repo id and LakeFS deletes asynchronously. It must now
    succeed straight away, with the renamed repo left intact.
    """
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    source_id = "owner/issue93-index"
    renamed_id = "owner/issue93-index-deprecate"

    await asyncio.to_thread(
        lambda: api.create_repo(repo_id=source_id, repo_type="dataset", private=True)
    )

    # A regular file and an LFS-sized one, so the rename exercises both the
    # re-upload and the physical-address linking paths (the test profile sets the
    # LFS threshold to 1 KiB).
    #
    # Both are passed as file-like objects rather than raw bytes on purpose:
    # huggingface_hub >= 1.0 routes byte payloads through Xet storage when
    # hf_xet is installed, and this server does not implement the Xet upload
    # endpoints. A buffer is explicitly unsupported by Xet, so the client falls
    # back to the plain HTTP/LFS path this test means to exercise.
    await asyncio.to_thread(
        lambda: api.upload_file(
            path_or_fileobj=io.BytesIO(b"# issue 93\n"),
            path_in_repo="README.md",
            repo_id=source_id,
            repo_type="dataset",
        )
    )
    await asyncio.to_thread(
        lambda: api.upload_file(
            path_or_fileobj=io.BytesIO(b"x" * 4096),
            path_in_repo="table.parquet",
            repo_id=source_id,
            repo_type="dataset",
        )
    )

    await asyncio.to_thread(
        lambda: api.move_repo(
            from_id=source_id, to_id=renamed_id, repo_type="dataset"
        )
    )

    # The freed name must be usable right away. Pre-fix this raised
    # HfHubHTTPError(500).
    await asyncio.to_thread(
        lambda: api.create_repo(repo_id=source_id, repo_type="dataset", private=True)
    )

    recreated = await asyncio.to_thread(
        lambda: api.repo_info(repo_id=source_id, repo_type="dataset")
    )
    # `siblings` is None rather than [] for an empty repo on huggingface_hub
    # < 1.0, and an empty repo is exactly what this asserts.
    recreated_files = {sibling.rfilename for sibling in (recreated.siblings or [])}
    assert "README.md" not in recreated_files, (
        "the recreated repo must be empty, not aliased onto the renamed one's data"
    )
    assert "table.parquet" not in recreated_files

    # And the renamed repo keeps its content.
    renamed = await asyncio.to_thread(
        lambda: api.repo_info(repo_id=renamed_id, repo_type="dataset")
    )
    renamed_files = {sibling.rfilename for sibling in (renamed.siblings or [])}
    assert {"README.md", "table.parquet"} <= renamed_files

    # The two repos must be backed by different LakeFS repositories, otherwise
    # writing to one would corrupt the other.
    from kohakuhub.db_operations import get_repository
    from kohakuhub.utils.lakefs import resolve_lakefs_repo

    recreated_row = get_repository("dataset", "owner", "issue93-index")
    renamed_row = get_repository("dataset", "owner", "issue93-index-deprecate")
    assert resolve_lakefs_repo(recreated_row) != resolve_lakefs_repo(renamed_row)

    # Writing to the recreated repo must not touch the renamed one.
    await asyncio.to_thread(
        lambda: api.upload_file(
            path_or_fileobj=io.BytesIO(b"fresh\n"),
            path_in_repo="NEW.md",
            repo_id=source_id,
            repo_type="dataset",
        )
    )
    renamed_after = await asyncio.to_thread(
        lambda: api.repo_info(repo_id=renamed_id, repo_type="dataset")
    )
    assert "NEW.md" not in {s.rfilename for s in (renamed_after.siblings or [])}


async def test_hf_move_repo_keeps_history_branches_and_tags(
    live_server_url,
    hf_api_token,
):
    """End-to-end reproduction of issue #107.

    A move used to recreate the LakeFS repository from the main head only, so a
    rename silently dropped the commit history, every other branch, and every
    tag. A move is now a rename of the KHub row: everything must survive, and
    the old name must still be free to reuse - repeatedly, since each rename
    keeps one more LakeFS id of that name taken for good.
    """
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    source_id = "owner/issue107-history"

    await asyncio.to_thread(
        lambda: api.create_repo(repo_id=source_id, repo_type="dataset", private=True)
    )
    # File-like payloads keep huggingface_hub >= 1.0 off the Xet upload path;
    # see test_hf_move_repo_frees_the_old_name_for_immediate_reuse.
    for path, payload, message in (
        ("README.md", b"# issue 107\n", "Add README"),
        ("data.bin", b"y" * 4096, "Add data"),
    ):
        await asyncio.to_thread(
            lambda: api.upload_file(
                path_or_fileobj=io.BytesIO(payload),
                path_in_repo=path,
                repo_id=source_id,
                repo_type="dataset",
                commit_message=message,
            )
        )
    await asyncio.to_thread(
        lambda: api.create_branch(repo_id=source_id, branch="qa-pass", repo_type="dataset")
    )
    await asyncio.to_thread(
        lambda: api.create_tag(repo_id=source_id, tag="v1", repo_type="dataset")
    )
    commits_before = await asyncio.to_thread(
        lambda: [c.title for c in api.list_repo_commits(repo_id=source_id, repo_type="dataset")]
    )

    moved_ids = []
    for cycle in range(3):
        moved_id = f"owner/issue107-history-moved-{cycle}"
        await asyncio.to_thread(
            lambda: api.move_repo(from_id=source_id, to_id=moved_id, repo_type="dataset")
        )
        moved_ids.append(moved_id)
        # The freed name is reusable straight away, every time.
        await asyncio.to_thread(
            lambda: api.create_repo(repo_id=source_id, repo_type="dataset", private=True)
        )

    first = moved_ids[0]
    refs = await asyncio.to_thread(lambda: api.list_repo_refs(repo_id=first, repo_type="dataset"))
    assert {branch.name for branch in refs.branches} == {"main", "qa-pass"}
    assert {tag.name for tag in refs.tags} == {"v1"}
    commits_after = await asyncio.to_thread(
        lambda: [c.title for c in api.list_repo_commits(repo_id=first, repo_type="dataset")]
    )
    assert commits_after == commits_before, "the move must not rewrite or drop history"
    readme = await asyncio.to_thread(
        lambda: Path(
            api.hf_hub_download(repo_id=first, filename="README.md", repo_type="dataset", revision="v1")
        ).read_bytes()
    )
    assert readme == b"# issue 107\n"

    # The later incarnations are independent, empty repositories...
    for moved_id in moved_ids[1:]:
        info = await asyncio.to_thread(lambda: api.repo_info(repo_id=moved_id, repo_type="dataset"))
        assert not {s.rfilename for s in (info.siblings or [])} & {"README.md", "data.bin"}

    # ...backed by their own LakeFS repositories: deleting the current holder
    # of the name leaves the moved repository untouched.
    await asyncio.to_thread(lambda: api.delete_repo(repo_id=source_id, repo_type="dataset"))
    data = await asyncio.to_thread(
        lambda: Path(
            api.hf_hub_download(repo_id=first, filename="data.bin", repo_type="dataset")
        ).read_bytes()
    )
    assert data == b"y" * 4096



async def test_hf_move_repo_refuses_a_name_that_normalizes_to_another_repo(
    live_server_url,
    hf_api_token,
):
    """Create refuses `My_Repo` next to `my-repo`; a move must refuse it too,
    while still allowing a repository to change the case of its own name."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    for repo_id in ("owner/issue108-norm", "owner/issue108-other"):
        await asyncio.to_thread(
            lambda: api.create_repo(repo_id=repo_id, repo_type="model", private=True)
        )

    # Status is read off the response so this holds across huggingface_hub
    # versions whose HTTP error classes live in different modules.
    with pytest.raises(Exception) as conflict:
        await asyncio.to_thread(
            lambda: api.move_repo(
                from_id="owner/issue108-other", to_id="owner/Issue108_Norm", repo_type="model"
            )
        )
    assert getattr(getattr(conflict.value, "response", None), "status_code", None) == 409

    await asyncio.to_thread(
        lambda: api.move_repo(
            from_id="owner/issue108-norm", to_id="owner/Issue108-Norm", repo_type="model"
        )
    )
    renamed = await asyncio.to_thread(
        lambda: api.repo_info(repo_id="owner/Issue108-Norm", repo_type="model")
    )
    assert renamed.id == "owner/Issue108-Norm"


def _sibs(info) -> dict:
    """``info.siblings`` by name, as plain dicts: huggingface_hub < 0.21 keeps
    the raw JSON, later releases ``RepoSibling`` objects."""

    def plain(sibling):
        if isinstance(sibling, dict):
            return {"size": sibling.get("size"), "blob_id": sibling.get("blobId"), "lfs": sibling.get("lfs")}
        lfs = sibling.lfs
        if lfs is not None and not isinstance(lfs, dict):
            lfs = {"size": lfs.size, "sha256": lfs.sha256}
        return {"size": sibling.size, "blob_id": sibling.blob_id, "lfs": lfs}

    return {
        (s["rfilename"] if isinstance(s, dict) else s.rfilename): plain(s) for s in info.siblings or []
    }


async def test_hf_api_repo_info_matches_the_hub_contract(live_server_url, hf_api_token):
    """What huggingface_hub reads back, as on the Hub: names only by default,
    blob fields with ``files_metadata``, only the asked properties with
    ``expand`` (on releases that have it)."""
    import inspect

    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    default = await asyncio.to_thread(lambda: api.model_info("owner/demo-model"))
    names = _sibs(default)
    assert set(names) >= {"README.md", "weights/model.safetensors"}
    assert all(v == {"size": None, "blob_id": None, "lfs": None} for v in names.values())
    assert default.sha and default.private is False

    detailed = await asyncio.to_thread(
        lambda: api.model_info("owner/demo-model", files_metadata=True)
    )
    by_name = _sibs(detailed)
    assert by_name["README.md"]["blob_id"] and by_name["README.md"]["size"] > 0
    weights = by_name["weights/model.safetensors"]
    assert weights["lfs"] is not None and weights["blob_id"]
    assert weights["lfs"]["size"] == weights["size"]

    if "expand" not in inspect.signature(api.model_info).parameters:
        return  # this huggingface_hub predates expand
    expanded = await asyncio.to_thread(
        lambda: api.model_info("owner/demo-model", expand=["sha", "lastModified", "likes"])
    )
    assert expanded.sha == default.sha and expanded.last_modified is not None
    assert expanded.siblings is None


async def test_hf_snapshot_download_lists_files_from_the_name_only_default(
    live_server_url, hf_api_token, tmp_path
):
    """snapshot_download enumerates files from repo_info's default sibling list
    (and asserts it is present on huggingface_hub < 0.36)."""
    local = await asyncio.to_thread(
        lambda: snapshot_download(
            "owner/demo-model",
            endpoint=live_server_url,
            token=hf_api_token,
            cache_dir=str(tmp_path / "cache"),
        )
    )
    files = {p.relative_to(local).as_posix() for p in Path(local).rglob("*") if p.is_file()}
    assert {"README.md", "weights/model.safetensors"} <= files


async def test_hf_paths_info_expands_a_directory_without_a_last_commit(live_server_url, hf_api_token):
    """A directory's last commit is not looked up (LakeFS would diff every
    commit of the history): huggingface_hub reads the null as none."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    (entry,) = await asyncio.to_thread(
        lambda: api.get_paths_info("owner/demo-model", ["weights"], expand=True)
    )

    assert entry.path == "weights" and entry.last_commit is None


async def test_hf_dataset_info_default_and_files_metadata(live_server_url, member_hf_api_token):
    api = HfApi(endpoint=live_server_url, token=member_hf_api_token)

    default = await asyncio.to_thread(lambda: api.dataset_info("acme-labs/private-dataset"))
    detailed = await asyncio.to_thread(
        lambda: api.dataset_info("acme-labs/private-dataset", files_metadata=True)
    )

    assert default.private is True
    assert list(_sibs(default)) == list(_sibs(detailed))
    assert all(v["size"] is None and v["blob_id"] is None for v in _sibs(default).values())
    train = _sibs(detailed)["data/train.jsonl"]
    assert train["size"] > 0 and train["blob_id"]


async def test_hf_repo_info_pinned_to_a_commit(live_server_url, hf_api_token, tmp_path):
    """A revision goes through /revision/{rev}: the same contract."""
    import inspect

    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    head = await asyncio.to_thread(lambda: api.model_info("owner/demo-model"))
    sha = (await asyncio.to_thread(lambda: api.list_repo_commits("owner/demo-model")))[0].commit_id

    pinned = await asyncio.to_thread(lambda: api.model_info("owner/demo-model", revision=sha))
    detailed = await asyncio.to_thread(
        lambda: api.model_info("owner/demo-model", revision=sha, files_metadata=True)
    )
    assert set(_sibs(pinned)) == set(_sibs(head))
    assert all(v["size"] is None for v in _sibs(pinned).values())
    weights = _sibs(detailed)["weights/model.safetensors"]
    assert weights["lfs"] is not None and weights["blob_id"]

    local = await asyncio.to_thread(
        lambda: snapshot_download(
            "owner/demo-model", revision=sha, endpoint=live_server_url,
            token=hf_api_token, cache_dir=str(tmp_path / "cache"),
        )
    )
    assert (Path(local) / "README.md").is_file()

    if "expand" not in inspect.signature(api.model_info).parameters:
        return
    expanded = await asyncio.to_thread(
        lambda: api.model_info("owner/demo-model", revision=sha, expand=["sha"])
    )
    assert expanded.sha and expanded.siblings is None


async def test_hf_unknown_expand_property_raises_a_bad_request(live_server_url, hf_api_token):
    import inspect

    from huggingface_hub.utils import HfHubHTTPError

    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    if "expand" not in inspect.signature(api.model_info).parameters:
        pytest.skip("this huggingface_hub predates expand")

    with pytest.raises(HfHubHTTPError) as raised:
        await asyncio.to_thread(lambda: api.model_info("owner/demo-model", expand=["bogus"]))

    assert raised.value.response.status_code == 400


async def test_hf_list_repo_tree_expands_directories(live_server_url, hf_api_token):
    import inspect

    from huggingface_hub.hf_api import RepoFolder

    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    if "expand" not in inspect.signature(api.list_repo_tree).parameters:
        pytest.skip("this huggingface_hub predates expand")

    entries = await asyncio.to_thread(lambda: list(api.list_repo_tree("owner/demo-model", expand=True)))

    folder = next(e for e in entries if isinstance(e, RepoFolder) and e.path == "weights")
    readme = next(e for e in entries if e.path == "README.md")
    assert folder.last_commit is None and readme.last_commit is not None
