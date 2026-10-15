"""Name normalization utilities.

Shared utilities for username/organization/repository name validation and normalization.
"""

import re


def normalize_name(name: str) -> str:
    """Normalize name for conflict checking.

    Names that normalize to the same value are considered conflicts.
    This prevents confusing names like 'My-Repo' and 'my_repo'.

    Args:
        name: Original name

    Returns:
        Normalized name (lowercase, hyphens/underscores removed)
    """
    # Convert to lowercase
    normalized = name.lower()
    # Remove hyphens and underscores
    normalized = normalized.replace("-", "").replace("_", "")
    return normalized


# The Hub's repository names, as ``huggingface_hub.utils.validate_repo_id`` checks them (it mirrors
# the Hub's own rule): 1 to 96 ASCII letters, digits, '_', '-' and '.', starting and ending with a
# letter, digit or '_'.
_REPO_NAME = re.compile(r"\w(?:[\w.-]{0,94}\w)?", re.ASCII)


def repo_name_problem(name: str) -> str | None:
    """What is wrong with ``name`` as a repository name, or ``None`` when the Hub would take it.

    ``fullmatch`` and not ``$``: a trailing newline is no part of a name. A name that is empty,
    holds a ``/`` (no route reaches it), is longer than the table holds, or ends in ``.git``
    (``name.git`` is the git URL of ``name``) is refused here rather than made.
    """
    if not _REPO_NAME.fullmatch(name):
        return (
            "it must be 1 to 96 letters, digits, '-', '_' or '.' (ASCII), "
            "and cannot start or end with '-' or '.'"
        )
    if "--" in name or ".." in name:
        return "it cannot contain '--' or '..'"
    if name.endswith(".git"):
        return "it cannot end with '.git'"
    return None
