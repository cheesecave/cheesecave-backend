"""Row factories for real-database tests. Defaults are valid; overrides are keyword args."""

from __future__ import annotations

from datetime import datetime

from kohakuhub.db import DailyRepoStats, FallbackSource, Repository, User


def make_user(username="owner", **overrides):
    values = {
        "username": username,
        "normalized_name": username.lower(),
        "email": f"{username}@example.com",
    }
    values.update(overrides)
    return User.create(**values)


def make_repo(owner, name, repo_type="model", private=False, **overrides):
    namespace = owner.username
    values = {
        "repo_type": repo_type,
        "namespace": namespace,
        "name": name,
        "full_id": f"{namespace}/{name}",
        "owner": owner,
        "private": private,
        "created_at": datetime(2025, 1, 1),
    }
    values.update(overrides)
    return Repository.create(**values)


def make_daily_stats(repo, date, download_sessions=0, **overrides):
    values = {
        "repository": repo,
        "date": date,
        "download_sessions": download_sessions,
        "authenticated_downloads": 0,
        "anonymous_downloads": download_sessions,
        "total_files": download_sessions,
        "created_at": datetime(2025, 1, 1),
    }
    values.update(overrides)
    return DailyRepoStats.create(**values)


def make_fallback_source(name="Mirror", **overrides):
    """A fallback source row; the default is an enabled global HuggingFace source."""
    values = {
        "namespace": "",
        "url": "https://huggingface.co",
        "priority": 100,
        "name": name,
        "source_type": "huggingface",
        "enabled": True,
    }
    values.update(overrides)
    return FallbackSource.create(**values)
