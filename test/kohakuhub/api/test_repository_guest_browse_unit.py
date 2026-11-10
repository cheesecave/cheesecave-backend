"""Full guest browser metadata remains separate from original-file access."""
import json
from datetime import timedelta

import pytest

from kohakuhub.api import repository_guest_preview as preview
from kohakuhub.db import Repository, RepositoryCard, RepositoryGuestSession, utcnow
from test.kohakuhub.api.test_repository_guest_preview_unit import HEAD, SOURCE_SECRET, browser
from test.kohakuhub.support.factories import make_commit, make_file


def test_overview_returns_safe_repository_identity_card_and_readme(browser):
    browser.contents["README.md"] = b"# Model card\n" + b"Visible documentation\n" * 100
    make_file(browser.repo, "README.md", "b" * 64, size=3000)
    make_file(browser.repo, "deleted.txt", "c" * 64, is_deleted=True)
    make_commit(browser.repo, HEAD, message="Main snapshot")
    result = browser.client.get(browser.base + "/overview")
    assert result.status_code == 200
    data = result.json()
    assert data["id"] == "owner/target" and data["type"] == "model"
    assert data["author"] == "owner" and data["private"] is True and data["invite_mode"] is True
    assert data["default_branch"] == "main" and data["head"] == HEAD
    assert data["file_count"] == 1 and data["last_modified"]
    assert data["created_at"].endswith("+00:00") and data["last_modified"].endswith("+00:00")
    assert data["readme"]["text"] == browser.contents["README.md"].decode()
    assert data["readme"]["truncated"] is False
    assert result.headers["cache-control"] == "no-store"
    for forbidden in (SOURCE_SECRET, "physical_address", "checksum", "lakefs_repo", "quota_bytes"):
        assert forbidden not in result.text
    browser.storage.get_object.assert_not_awaited()


def test_card_documentation_is_a_fixed_larger_root_only_prefix(browser):
    browser.contents["README.md"] = b"line\n" * 30000
    data = browser.client.get(browser.base + "/overview").json()["readme"]
    assert data["truncated"] is True
    assert len(data["text"].encode()) <= 65536 and len(data["text"].splitlines()) <= 1000
    browser.storage.get_object_prefix.assert_awaited_once_with("model-owner-target", HEAD, "README.md", 65537)
    assert browser.client.get(browser.base + "/overview", params={"path": "other.txt"}).status_code == 400
    ordinary = browser.client.get(browser.base + "/preview", params={"path": "README.md"}).json()
    assert len(ordinary["text"].splitlines()) <= 64


@pytest.mark.parametrize("state", ["missing", "binary", "lfs", "empty"])
def test_overview_handles_unavailable_readme_without_resolving_original(browser, state):
    if state == "missing":
        browser.contents.clear()
    elif state == "binary":
        browser.contents["README.md"] = b"\x00\xff"
    elif state == "lfs":
        make_file(browser.repo, "README.md", "b" * 64, lfs=True)
    else:
        browser.storage.get_branch.return_value = {"commit_id": None}
    result = browser.client.get(browser.base + "/overview")
    assert result.status_code == 200 and result.json()["readme"] is None
    browser.storage.get_object.assert_not_awaited()


def test_tree_complete_pagination_search_and_signed_scope(browser):
    objects = [{"path": f"docs/note-{i:03}.txt", "path_type": "object", "size_bytes": 9,
                "physical_address": SOURCE_SECRET} for i in range(205)]

    async def listing(**kwargs):
        assert kwargs["repository"] == "model-owner-target" and kwargs["ref"] == HEAD
        visible = [o for o in objects if o["path"].startswith(kwargs["prefix"]) and o["path"] > kwargs.get("after", "")]
        return {"results": visible[:kwargs["amount"]], "pagination": {"has_more": len(visible) > kwargs["amount"]}}

    browser.storage.list_objects.side_effect = listing
    query = {"path": "docs", "search": "note-"}
    first = browser.client.get(browser.base + "/tree", params=query)
    assert first.status_code == 200 and len(first.json()["entries"]) == 100
    assert first.json()["has_more"] is True
    cursor = first.json()["next_cursor"]
    second = browser.client.get(browser.base + "/tree", params={**query, "cursor": cursor}).json()
    third = browser.client.get(browser.base + "/tree", params={**query, "cursor": second["next_cursor"]}).json()
    paths = [row["path"] for page in (first.json(), second, third) for row in page["entries"]]
    assert len(paths) == len(set(paths)) == 205 and third["has_more"] is False
    assert SOURCE_SECRET not in first.text
    assert browser.client.get(browser.base + "/tree", params={"cursor": cursor}).status_code == 400
    assert browser.client.get(browser.base + "/tree", params={**query, "cursor": cursor + "x"}).status_code == 400
    browser.storage.get_branch.return_value = {"commit_id": "b" * 64}
    assert browser.client.get(browser.base + "/tree", params={**query, "cursor": cursor}).status_code == 409


@pytest.mark.parametrize("params", [{"search": "../"}, {"search": "x\\y"}, {"search": "x\n"},
                                   {"search": "x" * 129}, {"offset": "100"}, {"ref": "old"}, {"cursor": "bad"}])
def test_tree_rejects_unbounded_or_malformed_query(browser, params):
    assert browser.client.get(browser.base + "/tree", params=params).status_code == 400
    browser.storage.list_objects.assert_not_awaited()


def test_commit_pages_are_reachable_main_history_and_strip_internal_metadata(browser):
    commits = [{"id": f"{i:064x}", "message": f"Change {i}", "creation_date": 1700000000 + i,
                "committer": "Display author", "metadata": {"email": "private@example.com", "url": SOURCE_SECRET},
                "parents": ["f" * 64]} for i in range(1, 25)]

    async def log(**kwargs):
        assert kwargs["repository"] == "model-owner-target" and kwargs["ref"] == HEAD
        after = kwargs.get("after", "")
        rows = commits[commits.index(next(c for c in commits if c["id"] == after)) + 1:] if after else commits
        return {"results": rows[:kwargs["amount"]], "pagination": {"has_more": len(rows) > kwargs["amount"]}}

    browser.storage.log_commits.side_effect = log
    make_commit(browser.repo, commits[0]["id"], message="Tracked author")
    first = browser.client.get(browser.base + "/commits")
    assert first.status_code == 200
    data = first.json()
    assert len(data["commits"]) == 20 and data["has_more"] is True
    assert data["commits"][0]["author"] == "owner"
    assert set(data["commits"][0]) == {"id", "message", "timestamp", "author", "parents"}
    second = browser.client.get(browser.base + "/commits", params={"cursor": data["next_cursor"]}).json()
    assert len(second["commits"]) == 4 and second["has_more"] is False
    for forbidden in (SOURCE_SECRET, "private@example.com", "metadata"):
        assert forbidden not in first.text
    assert browser.client.get(browser.base + "/tree", params={"cursor": data["next_cursor"]}).status_code == 400


@pytest.mark.parametrize("endpoint", ["overview", "commits"])
def test_new_guest_routes_preserve_revocation_range_and_no_fallback_boundaries(browser, endpoint):
    assert browser.client.get(browser.base + "/" + endpoint, headers={"Range": "bytes=0-1"}).status_code == 400
    RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id == browser.guest.id).execute()
    assert browser.client.get(browser.base + "/" + endpoint).status_code == 404
    browser.storage.get_object.assert_not_awaited()


def test_new_guest_routes_reauthorize_after_storage_and_fail_closed(browser):
    async def revoke(**kwargs):
        RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id == browser.guest.id).execute()
        return {"results": [{"id": HEAD, "message": "Private", "creation_date": 1}], "pagination": {"has_more": False}}
    browser.storage.log_commits.side_effect = revoke
    result = browser.client.get(browser.base + "/commits")
    assert result.status_code == 404 and "Private" not in result.text


def test_card_metadata_exposes_only_bounded_safe_properties_at_current_head(browser):
    RepositoryCard.create(repository=browser.repo, main_sha=HEAD, card=json.dumps({
        "license": "mit", "language": ["en"], "physical_address": SOURCE_SECRET,
        "model-index": [{"name": "Evaluation", "url": SOURCE_SECRET, "score": float("nan"), "nested": {"token": "secret"}}],
        "tags": [SOURCE_SECRET], "unknown_internal": "not visible",
        "configs": {"enabled": True, "count": 3, "ratio": 0.5, "empty": None, "deep": [[[[[[[[0]]]]]]]]},
    }), derived="{}")
    metadata = browser.client.get(browser.base + "/overview").json()["metadata"]
    assert metadata["license"] == "mit" and metadata["language"] == ["en"]
    assert metadata["model-index"] == [{"name": "Evaluation", "score": None, "nested": {}}]
    assert metadata["tags"] == [None]
    assert metadata["configs"]["count"] == 3 and metadata["configs"]["enabled"] is True
    assert SOURCE_SECRET not in json.dumps(metadata) and "unknown_internal" not in metadata
    RepositoryCard.update(main_sha="b" * 64).where(RepositoryCard.repository == browser.repo.id).execute()
    assert browser.client.get(browser.base + "/overview").json()["metadata"] == {}


@pytest.mark.parametrize("card", ["not json", "[]", '"scalar"', "{" + '"license":"' + "x" * 65536 + '"}'],
                         ids=["invalid-json", "array", "scalar", "oversize"])
def test_unusable_card_index_never_breaks_the_repository_view(browser, card):
    RepositoryCard.create(repository=browser.repo, main_sha=HEAD, card=card, derived="{}")
    result = browser.client.get(browser.base + "/overview")
    assert result.status_code == 200 and result.json()["metadata"] == {}


@pytest.mark.parametrize("endpoint", ["overview", "commits"])
@pytest.mark.parametrize("params", [{"ref": "old"}, {"offset": "100"}, {"path": "private.txt"}, [("cursor", "a"), ("cursor", "b")]])
def test_new_browse_routes_reject_caller_selected_storage_queries(browser, endpoint, params):
    assert browser.client.get(browser.base + "/" + endpoint, params=params).status_code == 400
    browser.storage.get_branch.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["overview", "commits"])
@pytest.mark.parametrize("state", ["missing", "expired", "other-repository"])
def test_new_routes_never_use_unrelated_or_expired_guest_authority(browser, endpoint, state):
    if state == "missing":
        browser.client.cookies.clear()
    elif state == "expired":
        RepositoryGuestSession.update(expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryGuestSession.id == browser.guest.id).execute()
    else:
        browser.base = browser.base.replace("/target/", "/sibling/")
    assert browser.client.get(browser.base + "/" + endpoint).status_code == 404
    browser.storage.get_branch.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["tree", "commits"])
@pytest.mark.parametrize("listing", [None, {"results": None}, {"results": [], "pagination": None},
                                     {"results": [], "pagination": {"has_more": "yes"}},
                                     {"results": [], "pagination": {"has_more": True}}])
def test_browse_listings_fail_closed_when_storage_cannot_produce_a_valid_page(browser, endpoint, listing):
    operation = browser.storage.list_objects if endpoint == "tree" else browser.storage.log_commits
    operation.return_value = listing
    result = browser.client.get(browser.base + "/" + endpoint)
    assert result.status_code == 503 and SOURCE_SECRET not in result.text


def test_directory_page_cursor_preserves_the_directory_delimiter(browser):
    browser.storage.list_objects.side_effect = [
        {"results": [{"path": "docs/folder/", "path_type": "common_prefix"}], "pagination": {"has_more": True}},
        {"results": [{"path": "docs/z.txt", "path_type": "object", "size_bytes": 1}], "pagination": {"has_more": False}},
    ]
    page = browser.client.get(browser.base + "/tree", params={"path": "docs"}).json()
    assert page["entries"][0]["path"] == "docs/folder"
    second = browser.client.get(browser.base + "/tree", params={"path": "docs", "cursor": page["next_cursor"]})
    assert second.status_code == 200 and second.json()["entries"][0]["path"] == "docs/z.txt"
    assert browser.storage.list_objects.await_args.kwargs["after"] == "docs/folder/"


@pytest.mark.parametrize("author,expected", [("A Person <private@example.com>", "A Person"),
                                          ("private@example.com", "unknown"), (SOURCE_SECRET, "unknown"), (None, "unknown")])
def test_commit_display_authors_never_expose_email_or_internal_addresses(browser, author, expected):
    browser.storage.log_commits.return_value = {"results": [{"id": HEAD, "message": "Change", "creation_date": float("nan"),
                                                             "committer": author, "parents": ["invalid", "b" * 64, SOURCE_SECRET]}]}
    result = browser.client.get(browser.base + "/commits")
    assert result.status_code == 200
    commit = result.json()["commits"][0]
    assert commit["author"] == expected and commit["timestamp"] is None and commit["parents"] == ["b" * 64]
    assert "private@example.com" not in result.text and SOURCE_SECRET not in result.text


def test_commit_history_stops_at_the_repository_history_fence(browser):
    Repository.update(history_root="b" * 64).where(Repository.id == browser.repo.id).execute()
    browser.storage.log_commits.return_value = {"results": [
        {"id": HEAD, "message": "Current", "creation_date": 1, "parents": ["b" * 64]},
        {"id": "b" * 64, "message": "History root", "creation_date": 0, "parents": ["c" * 64]},
        {"id": "c" * 64, "message": "Removed history", "creation_date": 0},
    ], "pagination": {"has_more": True}}
    result = browser.client.get(browser.base + "/commits")
    assert result.status_code == 200
    assert result.json()["has_more"] is False and result.json()["next_cursor"] is None
    assert len(result.json()["commits"]) == 2 and result.json()["commits"][-1]["parents"] == []
    assert "Removed history" not in result.text and "c" * 64 not in result.text


@pytest.mark.parametrize("case", ["storage-error", "bad-stat", "wrong-path", "bad-prefix", "pointer", "bad-branch"])
def test_root_card_never_resolves_or_exposes_malformed_gateway_data(browser, case):
    expected = 503
    if case == "storage-error":
        browser.storage.stat_object.side_effect = RuntimeError(SOURCE_SECRET)
    elif case == "bad-stat":
        browser.storage.stat_object.side_effect = None
        browser.storage.stat_object.return_value = "invalid gateway object"
    elif case == "wrong-path":
        browser.metadata["README.md"] = {"path": "private-other.txt"}
        expected = 200
    elif case == "bad-prefix":
        browser.storage.get_object_prefix.side_effect = None
        browser.storage.get_object_prefix.return_value = "not bytes"
    elif case == "pointer":
        browser.contents["README.md"] = b"version https://git-lfs.github.com/spec/v1\noid sha256:private\n"
        expected = 200
    else:
        browser.storage.get_branch.return_value = None
    result = browser.client.get(browser.base + "/overview")
    assert result.status_code == expected and SOURCE_SECRET not in result.text
    if expected == 200:
        assert result.json()["readme"] is None
    browser.storage.get_object.assert_not_awaited()


def test_empty_main_has_no_commit_history_and_malformed_commit_ids_fail_closed(browser):
    browser.storage.get_branch.return_value = {"commit_id": None}
    assert browser.client.get(browser.base + "/commits").json() == {"commits": [], "has_more": False, "next_cursor": None}
    browser.storage.log_commits.assert_not_awaited()
    browser.storage.get_branch.return_value = {"commit_id": HEAD}
    browser.storage.log_commits.return_value = {"results": [{"id": SOURCE_SECRET}]}
    result = browser.client.get(browser.base + "/commits")
    assert result.status_code == 503 and SOURCE_SECRET not in result.text


@pytest.mark.parametrize("after", ["", None, "x" * 2049], ids=["empty-position", "wrong-type", "oversize-position"])
def test_malformed_signed_positions_are_rejected_before_a_page_is_read(browser, after):
    context = {"kind": "tree", "repository": browser.repo.id, "head": HEAD, "path": "", "search": ""}
    token = preview._cursor_token(context, after)
    assert browser.client.get(browser.base + "/tree", params={"cursor": token}).status_code == 400
    browser.storage.list_objects.assert_not_awaited()


def test_cursor_size_and_signed_storage_positions_are_independently_validated(browser):
    assert browser.client.get(browser.base + "/tree", params={"cursor": "x" * 8193}).status_code == 400
    tree = {"kind": "tree", "repository": browser.repo.id, "head": HEAD, "path": "docs", "search": ""}
    token = preview._cursor_token(tree, "other/private.txt")
    assert browser.client.get(browser.base + "/tree", params={"path": "docs", "cursor": token}).status_code == 400
    commits = {"kind": "commits", "repository": browser.repo.id, "head": HEAD, "history_root": None}
    token = preview._cursor_token(commits, "not-a-commit")
    assert browser.client.get(browser.base + "/commits", params={"cursor": token}).status_code == 400
    browser.storage.list_objects.assert_not_awaited()
    browser.storage.log_commits.assert_not_awaited()
