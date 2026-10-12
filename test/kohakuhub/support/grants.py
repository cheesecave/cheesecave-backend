"""Stand in for the batch call a real client makes before it commits an LFS file."""

from __future__ import annotations


def grant_lfs_upload(namespace: str, name: str, oid: str, repo_type: str = "model"):
    """Record that the repository was handed an upload for ``oid``, as the batch route does."""
    from kohakuhub.db import LfsGrant, Repository

    repo = Repository.get(
        (Repository.repo_type == repo_type)
        & (Repository.namespace == namespace)
        & (Repository.name == name)
    )
    return LfsGrant.get_or_create(repository=repo, sha256=oid, defaults={"kind": "upload"})[0]


def grant_lfs_upload_everywhere(oid: str):
    """Hand every repository an upload for ``oid``.

    For tests about retention, availability and history, where which repository
    uploaded an object is beside the point; scope tests name the repository.
    """
    from kohakuhub.db import LfsGrant, Repository

    for repo in Repository.select():
        LfsGrant.get_or_create(repository=repo, sha256=oid, defaults={"kind": "upload"})
