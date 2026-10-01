"""Runtime capabilities for operations that can mutate repository history."""

from typing import Literal

from fastapi import HTTPException

from kohakuhub import lakefs_compat
from kohakuhub.config import cfg

RepositoryOperation = Literal["revert", "reset", "squash"]

_OPERATION_CONFIG_FIELDS: dict[RepositoryOperation, str] = {
    "revert": "repository_revert_enabled",
    "reset": "repository_reset_enabled",
    "squash": "repository_squash_enabled",
}


def _configured() -> dict[str, bool]:
    """What the configuration allows, whatever LakeFS is."""
    # Keep this check aligned with db.py: any value other than the exact
    # configured PostgreSQL backend selects SQLite and cannot enable these
    # operations safely.
    if getattr(cfg.app, "db_backend", "sqlite") != "postgres":
        return {operation: False for operation in _OPERATION_CONFIG_FIELDS}

    return {
        operation: bool(getattr(cfg.app, config_field, False))
        for operation, config_field in _OPERATION_CONFIG_FIELDS.items()
    }


def get_repository_operation_capabilities() -> dict[str, bool]:
    """Return the effective public capabilities for dangerous operations."""
    capabilities = _configured()
    # Reset would go wrong silently on a LakeFS too old for it
    if not lakefs_compat.known().reset_supported:
        capabilities["reset"] = False
    return capabilities


def ensure_repository_operation_enabled(operation: RepositoryOperation) -> None:
    """Reject disabled history operations before they reach repository logic."""
    if get_repository_operation_capabilities()[operation]:
        return

    operation_name = operation.capitalize()
    if _configured()[operation]:  # then only LakeFS can have disabled it
        reason = f"{lakefs_compat.known().message} (see {lakefs_compat.DOCS})"
        error = message = f"Repository {operation_name} is disabled: {reason}"
    else:
        error = f"Repository {operation} is temporarily disabled"
        message = f"Repository {operation_name} is temporarily disabled"
    raise HTTPException(
        status_code=503,
        detail={
            "code": "operation_disabled",
            "operation": operation,
            "error": error,
            "message": message,
        },
    )


def require_repository_revert_enabled() -> None:
    """FastAPI dependency that gates Revert before authentication runs."""
    ensure_repository_operation_enabled("revert")


async def require_repository_reset_enabled() -> None:
    """FastAPI dependency that gates Reset before authentication runs.

    The LakeFS version is read first if this process has not learnt it yet
    (LakeFS may have been down when the service started).
    """
    await lakefs_compat.learn()
    ensure_repository_operation_enabled("reset")


def require_repository_squash_enabled() -> None:
    """FastAPI dependency that gates Super Squash before authentication runs."""
    ensure_repository_operation_enabled("squash")
