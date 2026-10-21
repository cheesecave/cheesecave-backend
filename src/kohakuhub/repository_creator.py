"""Who a repository's page names as its creator.

``repository_creator`` (migration 040) records the person who created a repository; ``Repository.owner`` is only
the account or organization the namespace names. A repository without a record was created before the creator was
written down: in a personal namespace only the account itself can have made it, in an organization nobody can say.
"""

from kohakuhub.api.utils.accounts import avatar_path
from kohakuhub.db import RepositoryCreator, User


def creator_profile(repo) -> dict | None:
    """The creator as a profile (``username``, ``full_name``, ``is_org``, ``avatar_url``), or ``None``."""
    row = RepositoryCreator.select(RepositoryCreator.user).where(RepositoryCreator.repository == repo.id).tuples().first()
    user_id = repo.owner_id if row is None else row[0]
    if user_id is None:  # the person's account is gone
        return None
    user = (
        User.select(
            User.username,
            User.full_name,
            User.is_org,
            User.avatar_updated_at,
            User.avatar.is_null(False).alias("has_avatar"),
        )
        .where(User.id == user_id)
        .first()
    )
    if row is None and user.is_org:  # an organization has no hands
        return None
    return {
        "username": user.username,
        "full_name": user.full_name,
        "is_org": user.is_org,
        "avatar_url": avatar_path(user.username, user.is_org, user.has_avatar, user.avatar_updated_at),
    }
