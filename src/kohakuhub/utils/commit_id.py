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
    """A commit id as the Hub says it: the first 40 characters of the full id.

    A shorter id (an abbreviation) is left as it is.
    """
    return commit_id[:HUB_LENGTH] if commit_id else commit_id


def refers_to(requested, full_id: str) -> bool:
    """Whether ``requested`` (a full id or a prefix of one, in either case) names the commit ``full_id``."""
    return is_commit_like(requested) and full_id.startswith(requested.lower())
