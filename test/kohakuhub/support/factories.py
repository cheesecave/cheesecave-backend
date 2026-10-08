"""Row factories for real-database tests. Defaults are valid; overrides are keyword args."""

from __future__ import annotations

from datetime import datetime

from kohakuhub.db import (
    Commit,
    DailyRepoStats,
    DownloadSession,
    File,
    FallbackSource,
    Repository,
    User,
    UserOrganization,
)


def make_user(username="owner", **overrides):
    values = {
        "username": username,
        "normalized_name": username.lower(),
        "email": f"{username}@example.com",
    }
    values.update(overrides)
    return User.create(**values)


def make_org(name, admin=None, **overrides):
    """An organization (a User with is_org=True), with unlimited quotas by default.

    ``admin`` becomes an admin member, so it can act in the namespace.
    """
    values = {
        "username": name,
        "normalized_name": name.lower(),
        "is_org": True,
        "email": None,
    }
    values.update(overrides)
    org = User.create(**values)
    if admin is not None:
        UserOrganization.create(user=admin, organization=org, role="admin")
    return org


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


def make_file(repo, path, sha256, size=0, lfs=False, **overrides):
    """A file row of ``repo`` at ``path``, owned by the repository's owner."""
    values = {
        "repository": repo,
        "owner_id": repo.owner_id,
        "path_in_repo": path,
        "sha256": sha256,
        "size": size,
        "lfs": lfs,
    }
    values.update(overrides)
    return File.create(**values)


def make_commit(repo, commit_id, author=None, branch="main", **overrides):
    """A commit row of ``repo`` made by ``author`` (default: the repository owner)."""
    author = author or repo.owner
    values = {
        "repository": repo,
        "commit_id": commit_id,
        "repo_type": repo.repo_type,
        "branch": branch,
        "author": author,
        "owner": repo.owner,
        "username": author.username,
        "message": f"commit {commit_id}",
    }
    values.update(overrides)
    return Commit.create(**values)


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


def make_download_session(repo, session_id, time_bucket=0, user=None, file_count=1, **overrides):
    """A download session of ``repo``; ``first_file`` and timestamps can be overridden."""
    values = {
        "repository": repo,
        "user": user,
        "session_id": session_id,
        "time_bucket": time_bucket,
        "file_count": file_count,
        "first_file": "file.bin",
    }
    values.update(overrides)
    return DownloadSession.create(**values)


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
