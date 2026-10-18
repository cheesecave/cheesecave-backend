"""One-time password reset links.

An administrator makes a link, hands it over out of band, and the person sets a new password with it. The token is 32
random bytes (64 hex characters); only its SHA3-512 hash is stored, like access tokens. A link works once, expires, and
a new link for the same account replaces the one before.
"""

from datetime import datetime, timedelta, timezone

from kohakuhub.auth.utils import generate_token, hash_token
from kohakuhub.db import PasswordResetToken, User

# key, seconds, label: from "on a call right now" to "will read it in a day or two". The administrator picks one when the
# link is made; the server accepts these and nothing else (not an arbitrary number, not longer than a week).
TTL_CHOICES = (
    ("15m", 15 * 60, "15 minutes"),
    ("1h", 60 * 60, "1 hour"),
    ("6h", 6 * 60 * 60, "6 hours"),
    ("24h", 24 * 60 * 60, "24 hours"),
    ("3d", 3 * 24 * 60 * 60, "3 days"),
    ("7d", 7 * 24 * 60 * 60, "7 days"),
)
DEFAULT_TTL = "1h"
TTL_KEYS = tuple(key for key, _, _ in TTL_CHOICES)


_SECONDS = {key: seconds for key, seconds, _ in TTL_CHOICES}


def ttl_seconds(key: str) -> int:
    return _SECONDS[key]  # KeyError for anything that is not a choice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    # SQLite and PostgreSQL hand back naive datetimes; they are UTC
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    """The same text whether the value was just made (aware) or read back (naive): UTC, ending in Z."""
    return _aware(value).isoformat().replace("+00:00", "Z")


def issue_link(user: User, ttl_key: str = DEFAULT_TTL, created_by: str = "admin") -> tuple[str, PasswordResetToken]:
    """Make a link for ``user`` and return ``(raw token, row)``; any unused link of the account is replaced."""
    raw = generate_token()
    with PasswordResetToken._meta.database.atomic():
        revoke_links(user)
        row = PasswordResetToken.create(
            user=user,
            token_hash=hash_token(raw),
            expires_at=_now() + timedelta(seconds=ttl_seconds(ttl_key)),
            created_by=created_by,
        )
    return raw, row


def find_valid(raw_token: str) -> PasswordResetToken | None:
    """The row for a token that is unused and has not expired, else ``None``."""
    row = PasswordResetToken.get_or_none(PasswordResetToken.token_hash == hash_token(raw_token))
    if row is None or row.used_at is not None or _aware(row.expires_at) <= _now():
        return None
    return row


def active_link(user: User) -> PasswordResetToken | None:
    return PasswordResetToken.get_or_none(
        (PasswordResetToken.user == user)
        & (PasswordResetToken.used_at.is_null())
        & (PasswordResetToken.expires_at > _now())
    )


def revoke_links(user: User) -> int:
    """Remove the unused links of ``user`` (used ones stay as history); the number removed."""
    return PasswordResetToken.delete().where((PasswordResetToken.user == user) & (PasswordResetToken.used_at.is_null())).execute()


def claim(row: PasswordResetToken) -> bool:
    """Use the link up. A conditional update, so of two requests with the same link exactly one gets ``True``."""
    return (
        PasswordResetToken.update(used_at=_now())
        .where(
            (PasswordResetToken.id == row.id)
            & (PasswordResetToken.used_at.is_null())
            & (PasswordResetToken.expires_at > _now())
        )
        .execute()
        == 1
    )
