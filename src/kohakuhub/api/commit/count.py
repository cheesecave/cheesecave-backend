"""Count the reachable history of an immutable LakeFS commit."""

import json

from kohakuhub.cache import cache_get_or_fetch


async def count_commit_history(client, lakefs_repo: str, head: str) -> int:
    """Only complete walks are cached; branch movement cannot alter a walk."""
    if not isinstance(head, str) or not head:
        raise RuntimeError("LakeFS returned no immutable commit head")
    key = "commit-count:" + json.dumps([lakefs_repo, head], separators=(",", ":"))
    return await cache_get_or_fetch(
        key, lambda: _walk_history(client, lakefs_repo, head), immutable=True, ttl=3600
    )


async def _walk_history(client, lakefs_repo: str, head: str) -> int:
    """Return a total only after LakeFS has answered every history page."""
    count, after = 0, None
    cursors = set()
    while True:
        page = await client.log_commits(repository=lakefs_repo, ref=head, amount=1000, after=after)
        commits, pagination = page["results"], page["pagination"]
        # A resolved head is itself a commit. Empty or malformed history is an
        # upstream failure, never proof of zero commits or a complete total.
        if not isinstance(commits, list) or not commits or any(
            not isinstance(commit, dict) or not isinstance(commit.get("id"), str) or not commit["id"]
            for commit in commits
        ):
            raise RuntimeError("LakeFS returned incomplete commit history")
        if not isinstance(pagination["has_more"], bool):
            raise RuntimeError("LakeFS returned invalid commit pagination")
        count += len(commits)
        if not pagination["has_more"]:
            return count
        after = pagination["next_offset"]
        if not isinstance(after, str) or not after or after in cursors:
            raise RuntimeError("LakeFS returned no next commit cursor")
        cursors.add(after)
