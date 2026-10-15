"""Which repository may reference which stored LFS object.

Objects are stored once for the whole instance, keyed by their sha256, so a
pointer in a commit, a ``verify`` call or an "already stored" answer must not
become a way to reach an object that belongs to somebody else. A repository may
reference an object when any of these holds:

* it already holds the object (a file, a history row or a branch-head link);
* it was handed an upload for the object (``LfsGrant`` of kind ``upload``), or
  proved the bytes of an object stored for repositories the caller cannot read
  (kind ``claim``, see below);
* the caller can read a repository that holds the object, so the content is
  theirs to attach anyway.

An object that is stored, but only for repositories the caller cannot read, is
not offered as "already stored" and is never overwritten: the upload goes to a
key private to the repository (``claim_key``) and counts once the bytes hash to
the object id.
"""

from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError

from kohakuhub.auth.permissions import RepoReadDeniedError, check_repo_read_permission
from kohakuhub.config import cfg
from kohakuhub.db import File, LFSObjectHistory, LfsGrant, LfsHeadRef, Repository
from kohakuhub.db_operations import repository_holds_lfs_object
from kohakuhub.lfs_gc import claim_key
from kohakuhub.logger import get_logger
from kohakuhub.utils.s3 import S3DeleteFailed, delete_objects_with_prefix, object_exists, object_sha256

logger = get_logger("LFS_SCOPE")


def grant_of(repo: Repository, oid: str) -> LfsGrant | None:
    return LfsGrant.get_or_none((LfsGrant.repository == repo) & (LfsGrant.sha256 == oid))


def record_grant(repo: Repository, oid: str, kind: str) -> LfsGrant:
    """Remember that ``repo`` was handed an upload (``upload``) or a claim."""
    grant, created = LfsGrant.get_or_create(
        repository=repo, sha256=oid, defaults={"kind": kind}
    )
    if not created and grant.kind == "claim" and kind == "upload":
        # The object is gone from storage since the claim was handed out, so
        # this repository now uploads it the ordinary way.
        grant.kind = "upload"
        grant.proven = False
        grant.save()
    return grant


def has_standing(repo: Repository, oid: str) -> bool:
    """The repository holds the object or was granted it, no caller involved."""
    if repository_holds_lfs_object(repo, oid):
        return True
    grant = grant_of(repo, oid)
    return grant is not None and (grant.kind == "upload" or grant.proven)


def readable_by(user, oid: str) -> bool:
    """True when ``user`` can read at least one repository that holds the object."""
    holder_ids = {
        row[0]
        for model in (File, LFSObjectHistory, LfsHeadRef)
        for row in model.select(model.repository).where(model.sha256 == oid).tuples()
    }
    for holder in Repository.select().where(Repository.id.in_(holder_ids)):
        try:
            check_repo_read_permission(holder, user)
        except RepoReadDeniedError:
            continue
        return True
    return False


async def prove_claim(repo: Repository, oid: str) -> bool:
    """Hash the bytes ``repo`` uploaded for a claim; the claim counts on a match.

    The uploaded copy is removed either way: the stored object is already
    there, and a mismatch is not content anybody may keep.
    """
    grant = grant_of(repo, oid)
    if grant is None or grant.kind != "claim":
        return False
    if grant.proven:
        return True
    key = claim_key(repo.id, oid)
    if not await object_exists(cfg.s3.bucket, key):
        return False
    matches = await object_sha256(cfg.s3.bucket, key) == oid
    if matches:
        grant.proven = True
        grant.save()
    else:
        logger.warning(f"Claimed bytes for {oid[:8]} do not hash to the object id")
    try:
        await delete_objects_with_prefix(cfg.s3.bucket, key)
    except (ClientError, BotoCoreError, S3DeleteFailed) as e:
        # the bytes are not content anybody may use; a later run of the expiry takes them
        logger.warning(f"Could not remove the uploaded copy for {oid[:8]}: {e}")
    return matches


async def may_reference(repo: Repository, user, oid: str) -> bool:
    """Whether ``repo``, acting for ``user``, may link / verify / commit ``oid``."""
    return (
        has_standing(repo, oid)
        or readable_by(user, oid)
        or await prove_claim(repo, oid)
    )
