"""The Hub's account routes: user and organization overview, organization members.

``huggingface_hub`` calls ``/api/users/{user}/overview``, ``/api/organizations/{org}/overview``
and ``/api/organizations/{org}/members``; the data is the same this hub shows on profile pages.
Counts are what the viewer may read, so a private repository is never counted for a stranger.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from peewee import fn

from kohakuhub.api.utils.accounts import avatar_url, utc_iso
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.auth.permissions import filter_readable_repositories
from kohakuhub.db import Repository, RepositoryLike, User, UserFollow, UserOrganization

router = APIRouter()


def _account(name: str, is_org: bool) -> User:
    account = User.get_or_none((User.username == name) & (User.is_org == is_org))
    if account is None:
        raise HTTPException(404, "Organization not found" if is_org else "User not found")
    return account


def _repo_counts(namespace: str, viewer) -> dict:
    counts = {"model": 0, "dataset": 0, "space": 0}
    rows = (
        filter_readable_repositories(Repository.select(Repository.repo_type, fn.COUNT(Repository.id).alias("n")), viewer, namespace)
        .group_by(Repository.repo_type)
        .tuples()
    )
    for repo_type, count in rows:
        counts[repo_type] = count
    return counts


def _follows(viewer, target) -> bool:
    return bool(
        viewer
        and UserFollow.select().where((UserFollow.follower == viewer.id) & (UserFollow.followed == target.id)).exists()
    )


def _hub_user(user: User) -> dict:
    return {
        "avatarUrl": avatar_url(user),
        "fullname": user.full_name or user.username,
        "isPro": False,
        "type": "user",
        "user": user.username,
    }


@router.get("/users/{username}/overview")
def user_overview(username: str, viewer: User | None = Depends(get_optional_user)):
    user = _account(username, is_org=False)
    counts = _repo_counts(user.username, viewer)
    orgs = [
        {"type": "org", "name": org.username, "fullname": org.full_name or org.username, "avatarUrl": avatar_url(org)}
        for org in User.select()
        .join(UserOrganization, on=(UserOrganization.organization == User.id))
        .where(UserOrganization.user == user)
        .order_by(User.username)
    ]
    return {
        "_id": str(user.id),
        "avatarUrl": avatar_url(user),
        "createdAt": utc_iso(user.created_at),
        "details": user.bio,
        "fullname": user.full_name or user.username,
        "isFollowing": _follows(viewer, user),
        "isPro": False,
        "numDatasets": counts["dataset"],
        "numDiscussions": 0,
        "numFollowers": UserFollow.select().where(UserFollow.followed == user.id).count(),
        "numFollowing": UserFollow.select().where(UserFollow.follower == user.id).count(),
        "numLikes": RepositoryLike.select().where(RepositoryLike.user == user.id).count(),
        "numModels": counts["model"],
        "numPapers": 0,
        "numSpaces": counts["space"],
        "numUpvotes": 0,
        "orgs": orgs,
        "type": "user",
        "user": user.username,
    }


@router.get("/organizations/{org_name}/overview")
def organization_overview(org_name: str, viewer: User | None = Depends(get_optional_user)):
    org = _account(org_name, is_org=True)
    counts = _repo_counts(org.username, viewer)
    return {
        "_id": str(org.id),
        "avatarUrl": avatar_url(org),
        "details": org.description,
        "fullname": org.full_name or org.username,
        "isFollowing": _follows(viewer, org),
        "isVerified": False,
        "name": org.username,
        "numDatasets": counts["dataset"],
        "numFollowers": UserFollow.select().where(UserFollow.followed == org.id).count(),
        "numModels": counts["model"],
        "numSpaces": counts["space"],
        "numUsers": UserOrganization.select().where(UserOrganization.organization == org.id).count(),
        "plan": None,
        "type": "org",
    }


@router.get("/organizations/{org_name}/members")
def organization_members(org_name: str):
    org = _account(org_name, is_org=True)
    members = (
        User.select()
        .join(UserOrganization, on=(UserOrganization.user == User.id))
        .where(UserOrganization.organization == org.id)
        .order_by(User.username)
    )
    return [_hub_user(member) for member in members]
