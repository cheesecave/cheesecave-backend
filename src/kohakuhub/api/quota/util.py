"""Storage quota utilities for KohakuHub with separate private/public quotas.

Enforces the quotas of users and organizations, private and public
repositories apart. Usage itself is kept by ``kohakuhub.usage``.
"""

from kohakuhub.db import Repository, User
from kohakuhub.db_operations import get_organization
from kohakuhub.logger import get_logger
from kohakuhub.usage import namespace_usage, namespace_used

logger = get_logger("QUOTA")


def check_quota(
    namespace: str, additional_bytes: int, is_private: bool, is_org: bool = False
) -> tuple[bool, str | None]:
    """Check if adding additional storage would exceed quota (SYNCHRONOUS).

    Args:
        namespace: Username or organization name
        additional_bytes: Bytes to be added
        is_private: True if uploading to a private repository
        is_org: True if namespace is an organization

    Returns:
        Tuple of (allowed: bool, error_message: str | None)
    """

    # Get current quota and usage - organizations are now users with is_org=True
    if is_org:
        entity = get_organization(namespace)
    else:
        entity = User.get_or_none(User.username == namespace)

    if not entity:
        return False, f"{'Organization' if is_org else 'User'} not found: {namespace}"

    quota_bytes = (
        entity.private_quota_bytes if is_private else entity.public_quota_bytes
    )
    quota_type = "private" if is_private else "public"

    # NULL quota = unlimited
    if quota_bytes is None:
        return True, None
    used_bytes = namespace_used(namespace, is_private)

    # Check if would exceed quota
    new_usage = used_bytes + additional_bytes

    if new_usage > quota_bytes:
        quota_gb = quota_bytes / (1000**3)
        new_usage_gb = new_usage / (1000**3)
        return (
            False,
            f"{quota_type.capitalize()} storage quota exceeded: {new_usage_gb:.2f}GB would exceed limit of {quota_gb:.2f}GB",
        )

    return True, None


def get_storage_info(
    namespace: str, is_org: bool = False
) -> dict[str, int | float | None]:
    """Get storage quota and usage information (SYNCHRONOUS).

    Args:
        namespace: Username or organization name
        is_org: True if namespace is an organization

    Returns:
        Dict with quota information for both private and public storage
    """

    # Organizations are now users with is_org=True
    if is_org:
        entity = get_organization(namespace)
    else:
        entity = User.get_or_none(User.username == namespace)

    private_quota = entity.private_quota_bytes if entity else None
    public_quota = entity.public_quota_bytes if entity else None
    used = namespace_usage([namespace])[namespace]
    private_used, public_used = used["private"], used["public"]

    # Calculate availability and percentages
    private_available = (
        None if private_quota is None else max(0, private_quota - private_used)
    )
    public_available = (
        None if public_quota is None else max(0, public_quota - public_used)
    )

    private_percentage = (
        None
        if private_quota is None or private_quota == 0
        else (private_used / private_quota * 100)
    )
    public_percentage = (
        None
        if public_quota is None or public_quota == 0
        else (public_used / public_quota * 100)
    )

    total_used = private_used + public_used

    return {
        "private_quota_bytes": private_quota,
        "public_quota_bytes": public_quota,
        "private_used_bytes": private_used,
        "public_used_bytes": public_used,
        "private_available_bytes": private_available,
        "public_available_bytes": public_available,
        "private_percentage_used": private_percentage,
        "public_percentage_used": public_percentage,
        "total_used_bytes": total_used,
    }


def set_quota(
    namespace: str,
    private_quota_bytes: int | None = None,
    public_quota_bytes: int | None = None,
    is_org: bool = False,
) -> dict[str, int | float | None]:
    """Set storage quota for a user or organization (SYNCHRONOUS).

    Args:
        namespace: Username or organization name
        private_quota_bytes: Private repo storage quota (None = unlimited, 0 = no change)
        public_quota_bytes: Public repo storage quota (None = unlimited, 0 = no change)
        is_org: True if namespace is an organization

    Returns:
        Updated storage info dict
    """

    # Organizations are now users with is_org=True
    if is_org:
        entity = get_organization(namespace)
    else:
        entity = User.get(User.username == namespace)

    if private_quota_bytes is not None:
        entity.private_quota_bytes = private_quota_bytes
    if public_quota_bytes is not None:
        entity.public_quota_bytes = public_quota_bytes
    entity.save(only=[User.private_quota_bytes, User.public_quota_bytes])

    logger.info(
        f"Set quota for {'org' if is_org else 'user'} {namespace}: "
        f"private={private_quota_bytes}, public={public_quota_bytes}"
    )

    return get_storage_info(namespace, is_org)


# ============================================================================
# Repository-specific quota management
# ============================================================================


def get_repo_storage_info(repo: Repository) -> dict[str, int | float | None]:
    """Get storage quota and usage information for a specific repository.

    Args:
        repo: Repository model instance

    Returns:
        Dict with quota information including namespace context
    """
    # Namespace context: the account the repository belongs to
    entity = repo.owner
    namespace_quota = (
        (entity.private_quota_bytes if repo.private else entity.public_quota_bytes)
        if entity
        else None
    )
    namespace_used_bytes = namespace_used(repo.namespace, repo.private)

    # Calculate namespace available quota
    namespace_available = (
        None
        if namespace_quota is None
        else max(0, namespace_quota - namespace_used_bytes)
    )

    # Repository quota and usage
    repo_quota = repo.quota_bytes
    repo_used = repo.used_bytes

    # Calculate effective quota (repo quota or namespace quota)
    effective_quota = repo_quota if repo_quota is not None else namespace_quota

    # Calculate availability
    available = None if effective_quota is None else max(0, effective_quota - repo_used)

    # Calculate percentage
    percentage = (
        None
        if effective_quota is None or effective_quota == 0
        else (repo_used / effective_quota * 100)
    )

    return {
        # Repository-specific
        "quota_bytes": repo_quota,
        "used_bytes": repo_used,
        "available_bytes": available,
        "percentage_used": percentage,
        # Effective quota (what's actually enforced)
        "effective_quota_bytes": effective_quota,
        # Namespace context
        "namespace_quota_bytes": namespace_quota,
        "namespace_used_bytes": namespace_used_bytes,
        "namespace_available_bytes": namespace_available,
        "is_inheriting": repo_quota is None,
    }


def set_repo_quota(
    repo: Repository, quota_bytes: int | None
) -> dict[str, int | float | None]:
    """Set storage quota for a repository with validation.

    Args:
        repo: Repository model instance
        quota_bytes: Quota in bytes (None = inherit from namespace)

    Returns:
        Updated storage info dict

    Raises:
        ValueError: If quota exceeds namespace available quota
    """
    # If setting a specific quota (not NULL), validate against namespace
    if quota_bytes is not None:
        entity = repo.owner  # the account the repository belongs to
        namespace_quota = (
            (entity.private_quota_bytes if repo.private else entity.public_quota_bytes)
            if entity
            else None
        )

        # Validate: repository quota cannot exceed namespace available
        if namespace_quota is not None:
            namespace_available = max(
                0, namespace_quota - namespace_used(repo.namespace, repo.private)
            )

            # Add back current repo quota if it was set (we're replacing it)
            if repo.quota_bytes is not None:
                namespace_available += repo.quota_bytes

            if quota_bytes > namespace_available:
                raise ValueError(
                    f"Repository quota ({quota_bytes / (1000**3):.2f}GB) exceeds "
                    f"namespace available quota ({namespace_available / (1000**3):.2f}GB)"
                )

    # Update repository quota
    repo.quota_bytes = quota_bytes
    repo.save(only=[Repository.quota_bytes])  # the usage counters move on their own

    logger.info(f"Set quota for repository {repo.full_id}: quota={quota_bytes} bytes")

    return get_repo_storage_info(repo)
