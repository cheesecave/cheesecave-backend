"""How an account is shown to other clients: avatar address, and the Hub's account fields."""

from __future__ import annotations

from datetime import datetime, timezone

from kohakuhub.config import cfg


def utc_iso(moment: datetime) -> str:
    """``2026-01-01T12:00:00.000000Z``, treating a naive timestamp as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def avatar_path(username: str, is_org: bool, has_avatar: bool, updated_at: datetime | None) -> str | None:
    """Site-relative address of an avatar (``None`` when the account has none)."""
    if not has_avatar:
        return None
    path = f"/api/{'organizations' if is_org else 'users'}/{username}/avatar?fallback=false"
    return path + "&v=" + utc_iso(updated_at) if updated_at else path


def absolute(path: str | None) -> str | None:
    return f"{cfg.app.base_url.rstrip('/')}{path}" if path else None


def avatar_url(user) -> str | None:
    """Absolute avatar address of a full ``User`` row, as the Hub's clients expect."""
    return absolute(avatar_path(user.username, user.is_org, user.avatar is not None, user.avatar_updated_at))
