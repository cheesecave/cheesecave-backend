"""Guest file headers expose current-main metadata without original-file access."""
from datetime import timedelta

import pytest

from kohakuhub.db import PathCommit, Repository, RepositoryGuestSession, utcnow
from test.kohakuhub.api.test_repository_guest_preview_unit import HEAD, SOURCE_SECRET, browser
from test.kohakuhub.support.factories import make_commit, make_file


def path_commit(browser, path="README.md", **overrides):
    values = dict(repository=browser.repo, branch="main", path=path,
                  commit_id=HEAD, title="Update documentation", date=1700000000)
    values.update(overrides)
    return PathCommit.create(**values)


def test_direct_file_header_uses_scoped_current_main_stat_and_no_original_content(browser):
    row = make_file(browser.repo, "README.md", "c" * 64, size=999)
    path_commit(browser)
    make_commit(browser.repo, HEAD, username="Display Name <private@example.com>")
    result = browser.client.get(browser.base + "/file-info", params={"path": "README.md"})
    assert result.status_code == 200
    assert result.json() == {
        "path": "README.md", "size": len(browser.contents["README.md"]),
        "last_modified": row.updated_at.replace(tzinfo=None).isoformat() + "+00:00",
        "last_commit": {"id": HEAD, "title": "Update documentation", "timestamp": 1700000000, "author": "Display Name"},
    }
    assert result.headers["cache-control"] == "no-store" and result.headers["vary"] == "Cookie"
    for forbidden in (SOURCE_SECRET, "checksum", "physical_address", "metadata", "private@example.com"):
        assert forbidden not in result.text
    browser.storage.stat_object.assert_awaited_once_with(repository="model-owner-target", ref=HEAD, path="README.md", user_metadata=False)
    browser.storage.get_object_prefix.assert_not_awaited()
    browser.storage.get_object.assert_not_awaited()


def test_unindexed_file_header_has_no_invented_commit_or_modified_time(browser):
    result = browser.client.get(browser.base + "/file-info", params={"path": "README.md"})
    assert result.status_code == 200
    assert result.json() == {"path": "README.md", "size": len(browser.contents["README.md"]), "last_modified": None}


@pytest.mark.parametrize("state", ["deleted", "missing", "empty-head", "different-path", "directory", "malformed"])
def test_file_header_fails_closed_for_unavailable_or_invalid_entries(browser, state):
    if state == "deleted":
        make_file(browser.repo, "README.md", "c" * 64, is_deleted=True)
    elif state == "missing":
        browser.contents.clear()
    elif state == "empty-head":
        browser.storage.get_branch.return_value = {"commit_id": None}
    elif state == "different-path":
        browser.metadata["README.md"] = {"path": "secret.txt"}
    elif state == "directory":
        browser.metadata["README.md"] = {"path_type": "common_prefix"}
    else:
        browser.storage.stat_object.side_effect = None
        browser.storage.stat_object.return_value = []
    result = browser.client.get(browser.base + "/file-info", params={"path": "README.md"})
    assert result.status_code == (503 if state == "malformed" else 404)
    browser.storage.get_object_prefix.assert_not_awaited()
    if state == "deleted":
        browser.storage.get_branch.assert_not_awaited()


@pytest.mark.parametrize("query", [{"path": "../README.md"}, {"path": ""}, {"path": "README.md", "ref": "old"},
                                  {"path": "README.md", "offset": "1"}, [("path", "README.md"), ("path", "other.txt")]])
def test_file_header_rejects_paths_revisions_ranges_and_offsets(browser, query):
    assert browser.client.get(browser.base + "/file-info", params=query).status_code == 400
    browser.storage.get_branch.assert_not_awaited()


def test_file_header_rejects_range_without_fetching_storage(browser):
    assert browser.client.get(browser.base + "/file-info", params={"path": "README.md"}, headers={"Range": "bytes=0-1"}).status_code == 400
    browser.storage.get_branch.assert_not_awaited()


@pytest.mark.parametrize("state", ["missing", "expired", "revoked", "sibling"])
def test_file_header_never_uses_unrelated_or_invalid_guest_authority(browser, state):
    if state == "missing":
        browser.client.cookies.clear()
    elif state == "sibling":
        browser.base = browser.base.replace("/target/", "/sibling/")
    else:
        RepositoryGuestSession.update(**({"expires_at": utcnow() - timedelta(seconds=1)} if state == "expired" else {"revoked_at": utcnow()})).where(RepositoryGuestSession.id == browser.guest.id).execute()
    assert browser.client.get(browser.base + "/file-info", params={"path": "README.md"}).status_code == 404
    browser.storage.get_branch.assert_not_awaited()


def test_file_header_reauthorizes_after_storage_revocation(browser):
    async def revoke(**kwargs):
        RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id == browser.guest.id).execute()
        return {"path": "README.md", "size_bytes": 10}
    browser.storage.stat_object.side_effect = revoke
    result = browser.client.get(browser.base + "/file-info", params={"path": "README.md"})
    assert result.status_code == 404 and "last_commit" not in result.text


@pytest.mark.parametrize("state", ["other-repo", "other-branch", "invalid-id", "pruned", "email-only", "no-author", "root", "head"])
def test_path_commit_metadata_is_scoped_sanitized_and_respects_history_fence(browser, state):
    values = {}
    if state == "other-repo":
        values["repository"] = browser.sibling
    elif state == "other-branch":
        values["branch"] = "private-branch"
    elif state == "invalid-id":
        values["commit_id"] = "invalid"
    elif state in {"pruned", "root", "head"}:
        Repository.update(history_root="b" * 64).where(Repository.id == browser.repo.id).execute()
        values["commit_id"] = {"pruned": "c" * 64, "root": "b" * 64, "head": HEAD}[state]
    path_commit(browser, **values)
    if state != "no-author":
        make_commit(browser.repo, values.get("commit_id", HEAD), username="private@example.com" if state == "email-only" else "owner")
    # A sibling commit with the same ID must not supply the author.
    make_commit(browser.sibling, HEAD, username="SiblingSecret")
    browser.storage.list_objects.return_value = {"results": [{"path": "README.md", "path_type": "object", "size_bytes": 10}]}
    file_info = browser.client.get(browser.base + "/file-info", params={"path": "README.md"}).json()
    entry = browser.client.get(browser.base + "/tree").json()["entries"][0]
    if state in {"other-repo", "other-branch", "invalid-id", "pruned"}:
        assert "last_commit" not in file_info and "last_commit" not in entry
    else:
        assert file_info["last_commit"] == entry["last_commit"]
        assert file_info["last_commit"]["author"] == ("unknown" if state in {"email-only", "no-author"} else "owner")
    assert "SiblingSecret" not in str(file_info) and "private@example.com" not in str(file_info)


def test_tree_folder_commit_and_title_are_bounded_whitelisted_metadata(browser):
    path_commit(browser, path="docs", title="X" * 17000)
    browser.storage.list_objects.return_value = {"results": [{"path": "docs/", "path_type": "common_prefix", "physical_address": SOURCE_SECRET}]}
    entry = browser.client.get(browser.base + "/tree").json()["entries"][0]
    assert entry["type"] == "directory" and len(entry["last_commit"]["title"]) == 16000
    assert set(entry["last_commit"]) == {"id", "title", "timestamp", "author"}
