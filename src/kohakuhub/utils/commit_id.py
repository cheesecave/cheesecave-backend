"""Commit ids in the forms people use them (#12).

LakeFS names a commit by a content address of 64 hex. The Hub, so ``huggingface_hub``, names it by 40 hex (a git
commit id) and takes any prefix of 5 to 40 (``parent_commit``, a revision, a ``snapshot_download``). Inside, a
commit is always its full 64 hex id; this is the syntax of the other forms, and nothing else: it imports nothing
of this package, so any module may use it. Turning a prefix into the commit it names takes LakeFS or the
database, and happens where the request is read.
"""

import re

FULL_LENGTH = 64  # what LakeFS says
HUB_LENGTH = 40  # what the Hub says

_COMMIT_ID = re.compile(rf"[0-9a-f]{{5,{FULL_LENGTH}}}", re.IGNORECASE)


def is_commit_like(value) -> bool:
    """Whether ``value`` can be a commit id or an abbreviation of one: 5 to 64 hex characters."""
    return isinstance(value, str) and _COMMIT_ID.fullmatch(value) is not None


def hub_oid(commit_id: str | None) -> str | None:
    """A commit id as the site says it (``commit_id_format``).

    ``hub`` (the default): the first 40 characters of the full id, which is what the Hub says and what
    ``huggingface_hub`` accepts as a ``parent_commit`` and names its cache folders by. ``full``: the id as
    LakeFS has it, a transitional form. An abbreviation is left as it is either way.
    """
    if not commit_id:
        return commit_id
    from kohakuhub.config import cfg  # read when called: the config is registered after this module is imported

    return commit_id if cfg.app.commit_id_format == "full" else commit_id[:HUB_LENGTH]


def hub_oids(commit_ids) -> list[str]:
    """``hub_oid`` of each id of a list (``None`` is an empty list)."""
    return [hub_oid(commit_id) for commit_id in commit_ids or []]


def hub_metadata(metadata: dict | None) -> dict:
    """The metadata of a commit with the ids it holds said as the site says them.

    LakeFS keeps what a commit's maker wrote: a revert writes the commit it undid (``revert_of``) as a full id.
    A value that is a full commit id is said by ``hub_oid``; anything else is left as it is.
    """
    return {
        key: hub_oid(value) if isinstance(value, str) and len(value) == FULL_LENGTH and is_commit_like(value) else value
        for key, value in (metadata or {}).items()
    }


def refers_to(requested, full_id: str) -> bool:
    """Whether ``requested`` (a full id or a prefix of one, in either case) names the commit ``full_id``."""
    return is_commit_like(requested) and full_id.startswith(requested.lower())
