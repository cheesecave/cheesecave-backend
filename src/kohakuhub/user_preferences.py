"""What a reader chose for themselves, kept on the server so it follows them to every browser.

Only keys that were set are stored; a key that is missing means "follow the site's default", which the
frontend resolves (reader's choice, then the site's, then the built-in). Setting a key to null removes it.
"""

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from kohakuhub.db import User, UserPreference
from kohakuhub.site_appearance import MarkdownStyleId



class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown_style: Optional[MarkdownStyleId] = None


def get_preferences(user: User) -> dict:
    """The keys the account has set; a row that cannot be read counts as no choice."""
    row = UserPreference.get_or_none(UserPreference.user == user)
    if row is None:
        return {}
    try:
        stored = Preferences.model_validate(json.loads(row.data))
    except (ValueError, ValidationError):
        return {}
    return stored.model_dump(exclude_none=True)


def update_preferences(user: User, patch: dict) -> dict:
    """Merge ``patch`` (keys already validated) into what is kept; null removes a key. Returns the result."""
    merged = {**get_preferences(user), **patch}
    merged = {key: value for key, value in merged.items() if value is not None}
    if not merged:
        UserPreference.delete().where(UserPreference.user == user).execute()
        return {}
    data = json.dumps(merged, sort_keys=True)
    UserPreference.insert(user=user, data=data).on_conflict(
        conflict_target=[UserPreference.user], update={UserPreference.data: data}
    ).execute()
    return merged
