"""How a revision followed by a path is told apart from a branch with a slash, and how a new ref name is checked (#67).

Pure logic over a LakeFS client that answers from a dict: nothing here reads the database. The same functions run
against real LakeFS in `api/test_slash_ref_names.py`.
"""

import httpx
import pytest

from kohakuhub.lakefs_rest_client import _named
from kohakuhub.utils.lakefs import (
    longer_revision,
    ref_name_conflict,
    resolve_revision_and_path,
)


class Refs:
    """Branches and tags by Hub name; `prefix` and paging as LakeFS does them."""

    def __init__(self, branches=(), tags=(), page=1000, broken=False):
        self.branches, self.tags, self.page, self.broken = list(branches), list(tags), page, broken
        self.listed = []

    async def _list(self, names, after, amount, prefix):
        if self.broken:
            raise RuntimeError("LakeFS is down")
        names = sorted(n for n in names if n.startswith(prefix or ""))
        self.listed.append((prefix, after))
        start = names.index(after) + 1 if after else 0
        chunk = names[start : start + min(amount or self.page, self.page)]
        more = start + len(chunk) < len(names)
        return {
            "results": [{"id": n, "commit_id": "c"} for n in chunk],
            "pagination": {"has_more": more, "next_offset": chunk[-1] if more else ""},
        }

    async def list_branches(self, repository, after=None, amount=None, prefix=None):
        return await self._list(self.branches, after, amount, prefix)

    async def list_tags(self, repository, after=None, amount=None, prefix=None):
        return await self._list(self.tags, after, amount, prefix)

    async def get_branch(self, repository, branch):
        return await self._get(self.branches, branch)

    async def get_tag(self, repository, tag):
        return await self._get(self.tags, tag)

    async def _get(self, names, name):
        if self.broken:
            request = httpx.Request("GET", "http://lakefs/x")
            raise httpx.HTTPStatusError("down", request=request, response=httpx.Response(500, request=request))
        if name not in names:
            request = httpx.Request("GET", "http://lakefs/x")
            raise httpx.HTTPStatusError("none", request=request, response=httpx.Response(404, request=request))
        return {"id": name, "commit_id": "c"}


def resolver(known):
    async def resolve(client, lakefs_repo, revision, repo=None):
        if revision not in known:
            raise ValueError(f"Revision '{revision}' not found")
        return f"commit-of-{revision}", {"id": f"commit-of-{revision}"}

    return resolve


async def test_a_listing_of_refs_is_followed_over_its_pages():
    client = Refs(branches=[f"feature/{i:03d}" for i in range(5)], page=2)
    found = await longer_revision(client, "r", "feature", "004/file.txt")
    assert found == ("feature/004", "file.txt")
    assert len([1 for p, _ in client.listed if p == "feature/"]) >= 3  # paged by two


async def test_the_longest_branch_or_tag_that_starts_the_path_wins():
    client = Refs(branches=["a/b"], tags=["a/b/c"])
    assert await longer_revision(client, "r", "a", "b/c/f.txt") == ("a/b/c", "f.txt")
    assert await longer_revision(client, "r", "a", "b/f.txt") == ("a/b", "f.txt")
    assert await longer_revision(client, "r", "a", "b") == ("a/b", "")  # the revision alone, no path
    assert await longer_revision(client, "r", "a", "x/f.txt") is None
    assert await longer_revision(client, "r", "a", "") is None


async def test_a_failure_of_lakefs_is_no_longer_revision():
    assert await longer_revision(Refs(broken=True), "r", "a", "b/f.txt") is None


async def test_the_usual_revision_costs_one_lookup_and_no_listing():
    client = Refs(branches=["main"])
    got = await resolve_revision_and_path(resolver({"main"}), client, "r", None, "main", "dir/f.txt")
    assert got == ("commit-of-main", {"id": "commit-of-main"}, "main", "dir/f.txt")
    assert client.listed == []


async def test_a_branch_with_a_slash_is_found_when_the_first_part_is_not_a_revision():
    client = Refs(branches=["feature/x"])
    got = await resolve_revision_and_path(resolver({"feature/x"}), client, "r", None, "feature", "x/dir/f.txt")
    assert got[2:] == ("feature/x", "dir/f.txt")


async def test_a_branch_that_starts_with_a_short_hex_part_wins_over_the_commit_abbreviation():
    client = Refs(branches=["a/b/c"])
    # `a` alone is a commit abbreviation to LakeFS; the branch `a/b/c` is what the path means
    got = await resolve_revision_and_path(resolver({"a", "a/b/c"}), client, "r", None, "a", "b/c/f.txt")
    assert got[2:] == ("a/b/c", "f.txt")


async def test_a_short_hex_part_that_starts_no_branch_is_the_commit_abbreviation():
    client = Refs(branches=["main"])
    got = await resolve_revision_and_path(resolver({"cafe"}), client, "r", None, "cafe", "f.txt")
    assert got[2:] == ("cafe", "f.txt")
    assert [p for p, _ in client.listed] == ["cafe/", "cafe/"]  # asked once for branches, once for tags


async def test_a_full_commit_id_is_never_asked_about():
    client = Refs(branches=["x"])
    sha = "a" * 40
    got = await resolve_revision_and_path(resolver({sha}), client, "r", None, sha, "f.txt")
    assert got[2:] == (sha, "f.txt")
    assert client.listed == []


async def test_nothing_found_is_the_resolvers_own_error():
    client = Refs(branches=["main"])
    with pytest.raises(ValueError, match="'nope' not found"):
        await resolve_revision_and_path(resolver({"main"}), client, "r", None, "nope", "x/f.txt")
    with pytest.raises(ValueError, match="'cafe' not found"):  # short hex, asked once only
        await resolve_revision_and_path(resolver({"main"}), client, "r", None, "cafe", "f.txt")


@pytest.mark.parametrize("kind", ["branch", "tag"])
async def test_a_name_inside_another_or_holding_one_conflicts(kind):
    existing = ["feat", "d/e/f"]
    client = Refs(branches=existing if kind == "branch" else [], tags=existing if kind == "tag" else [])
    assert await ref_name_conflict(client, "r", "feat/x", kind) == "feat"
    assert await ref_name_conflict(client, "r", "feat/x/y", kind) == "feat"
    assert await ref_name_conflict(client, "r", "d", kind) == "d/e/f"
    assert await ref_name_conflict(client, "r", "d/e", kind) == "d/e/f"
    assert await ref_name_conflict(client, "r", "d/e/g", kind) is None
    assert await ref_name_conflict(client, "r", "plain", kind) is None


async def test_a_failure_of_lakefs_is_not_a_missing_directory():
    with pytest.raises(httpx.HTTPStatusError):
        await ref_name_conflict(Refs(broken=True), "r", "a/b", "branch")


def test_only_the_ids_of_refs_are_named_again():
    assert _named({"id": "a_-2F-_b", "commit_id": "c"}) == {"id": "a/b", "commit_id": "c"}
    assert _named({"results": [{"id": "x_-2F-_y"}, {"id": "z"}], "pagination": {"next_offset": "x_-2F-_y"}}) == {
        "results": [{"id": "x/y"}, {"id": "z"}],
        "pagination": {"next_offset": "x_-2F-_y"},  # LakeFS's own cursor, handed back as it is
    }
    assert _named([{"id": "kept"}]) == [{"id": "kept"}]
    assert _named({"no": "id"}) == {"no": "id"}
