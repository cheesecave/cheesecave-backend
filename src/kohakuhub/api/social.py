"""Local follow relationships and permission-filtered, derived workspace activity."""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from peewee import SqliteDatabase, fn

from kohakuhub.api.repo.routers.info import _latest_main_commits
from kohakuhub.auth.dependencies import get_current_user, get_optional_user
from kohakuhub.auth.permissions import (
    compile_repository_predicate,
    repository_owner_interest,
    repository_read_predicate,
)
from kohakuhub.db import Repository, User, UserFollow, UserOrganization

router = APIRouter()


def _database():
    return UserFollow._meta.database


@contextmanager
def _read_snapshot():
    database = _database()
    with database.atomic():
        if not isinstance(database, SqliteDatabase):
            database.execute_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        yield


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(column):
    # Existing timestamps are UTC, including legacy timezone-naive PostgreSQL rows.
    if isinstance(_database(), SqliteDatabase):
        return f"CAST(ROUND((julianday({column}) - 2440587.5) * 86400000) AS INTEGER)"
    return f"CAST(ROUND(EXTRACT(EPOCH FROM {column} AT TIME ZONE 'UTC') * 1000) AS BIGINT)"


def _encode(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode(cursor, context):
    try:
        if len(cursor) > 2048:
            raise ValueError()
        value = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(value, dict) or value.get("v") != 1 or value.get("context") != context:
            raise ValueError()
        for key, length in (("last", 3), ("caps", 3)):
            if not isinstance(value.get(key), list) or len(value[key]) != length:
                raise ValueError()
            if any(type(item) is not int or item < 0 or item > 2**63 - 1 for item in value[key]):
                raise ValueError()
        if value["last"][1] > 2:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise HTTPException(422, "Invalid activity cursor") from None


def _target(username):
    target = User.get_or_none(User.username == username)
    if target is None:
        raise HTTPException(404, "Local account not found")
    return target


def _individual(user):
    if user.is_org:
        raise HTTPException(403, "Organizations cannot follow accounts")
    if not user.is_active:
        raise HTTPException(403, "Account is inactive")


def _profile(user):
    avatar = None
    if user.has_avatar:
        prefix = "organizations" if user.is_org else "users"
        avatar = f"/api/{prefix}/{user.username}/avatar?fallback=false"
        if user.avatar_updated_at:
            avatar += "&v=" + _utc(user.avatar_updated_at)
    return {
        "username": user.username,
        "full_name": user.full_name,
        "is_org": user.is_org,
        "avatar_url": avatar,
    }


def _profiles(ids):
    # Do not fetch avatar blobs or private profile fields for an activity page.
    return {
        user.id: _profile(user)
        for user in User.select(
            User.id,
            User.username,
            User.full_name,
            User.is_org,
            User.avatar_updated_at,
            User.avatar.is_null(False).alias("has_avatar"),
        ).where(User.id.in_(ids))
    }


def _state(target, viewer):
    following = bool(
        viewer
        and UserFollow.select()
        .where((UserFollow.follower == viewer.id) & (UserFollow.followed == target.id))
        .exists()
    )
    return {
        "username": target.username,
        "is_org": target.is_org,
        "following": following,
        "can_follow": bool(
            viewer
            and not viewer.is_org
            and viewer.is_active
            and target.is_active
            and viewer.id != target.id
        ),
        "followers_count": UserFollow.select().where(UserFollow.followed == target.id).count(),
        "following_count": UserFollow.select().where(UserFollow.follower == target.id).count(),
    }


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"


@router.get("/users/{username}/follow")
def follow_state(username: str, response: Response, user: User | None = Depends(get_optional_user)):
    _no_store(response)
    return _state(_target(username), user)


@router.put("/users/{username}/follow")
def follow(username: str, response: Response, user: User = Depends(get_current_user)):
    _individual(user)
    target = _target(username)
    if user.id == target.id:
        raise HTTPException(400, "Cannot follow yourself")
    if not target.is_active:
        raise HTTPException(400, "Cannot follow an inactive account")
    with _database().atomic():
        UserFollow.insert(follower=user.id, followed=target.id).on_conflict_ignore().execute()
        result = _state(target, user)
    _no_store(response)
    return result


@router.delete("/users/{username}/follow")
def unfollow(username: str, response: Response, user: User = Depends(get_current_user)):
    _individual(user)
    target = _target(username)
    if user.id == target.id:
        raise HTTPException(400, "Cannot follow yourself")
    with _database().atomic():
        UserFollow.delete().where(
            (UserFollow.follower == user.id) & (UserFollow.followed == target.id)
        ).execute()
        result = _state(target, user)
    _no_store(response)
    return result


def _list_follow(username, direction, limit, cursor, viewer):
    target = _target(username)
    context = ["follow", direction, target.id, viewer.id if viewer else None]
    own, other = (
        ("followed_id", "follower_id")
        if direction == "followers"
        else ("follower_id", "followed_id")
    )
    p = _database().param
    epoch = _epoch("created_at")
    with _read_snapshot():
        field = UserFollow.followed if direction == "followers" else UserFollow.follower
        cap = UserFollow.select(fn.MAX(UserFollow.id)).where(field == target.id).scalar() or 0
        state = (
            _decode(cursor, context)
            if cursor
            else {"v": 1, "context": context, "caps": [cap, 0, 0]}
        )
        params = [target.id, state["caps"][0]]
        where = f"{own} = {p} AND id <= {p}"
        if cursor:
            stamp, _, row_id = state["last"]
            where += f" AND ({epoch} < {p} OR ({epoch} = {p} AND id < {p}))"
            params += [stamp, stamp, row_id]
        rows = (
            _database()
            .execute_sql(
                f"SELECT id,{other},created_at,{epoch} AS stamp FROM user_follow WHERE {where} ORDER BY stamp DESC,id DESC LIMIT {p}",
                params + [limit + 1],
            )
            .fetchall()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        profiles = _profiles([row[1] for row in rows])
        items = [{**profiles[row[1]], "followed_at": _utc(row[2])} for row in rows]
        next_cursor = (
            _encode({**state, "last": [rows[-1][3], 0, rows[-1][0]]}) if has_more else None
        )
        return {"items": items, "has_more": has_more, "next_cursor": next_cursor}


@router.get("/users/{username}/followers")
def followers(
    username: str,
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    user: User | None = Depends(get_optional_user),
):
    _no_store(response)
    return _list_follow(username, "followers", limit, cursor, user)


@router.get("/users/{username}/following")
def following(
    username: str,
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    user: User | None = Depends(get_optional_user),
):
    _no_store(response)
    return _list_follow(username, "following", limit, cursor, user)


def _feed(user, scope, repo_type, limit, cursor, organization=None, event_type="all"):
    if scope == "organization":
        if not organization:
            raise HTTPException(400, "Organization scope requires an organization")
    elif organization is not None:
        raise HTTPException(400, "Organization is only valid with organization scope")
    database = _database()
    p = database.param
    # Changed actor attribution must invalidate older paging sessions as well.
    context = ["feed", user.id, scope, repo_type, "actor-v2", event_type]
    followed = f"SELECT followed_id FROM user_follow WHERE follower_id = {p}"
    followed_people = (
        f'SELECT f.followed_id FROM user_follow f JOIN "user" u ON u.id = f.followed_id '
        f"WHERE f.follower_id = {p} AND u.is_org = FALSE"
    )
    followed_orgs = (
        f'SELECT f.followed_id FROM user_follow f JOIN "user" u ON u.id = f.followed_id '
        f"WHERE f.follower_id = {p} AND u.is_org = TRUE"
    )
    repository = Repository.alias("r")
    visible, visible_params = compile_repository_predicate(
        repository_read_predicate(user, repository), database
    )
    own, own_params = compile_repository_predicate(
        repository_owner_interest(user, repository), database
    )
    with _read_snapshot():
        org = None
        if scope == "organization":
            # This resolves the requested member-owned namespace even when it has
            # no repositories. Following it never satisfies this membership check.
            org = (
                User.select(User.id)
                .join(UserOrganization, on=(UserOrganization.organization == User.id))
                .where(
                    (User.username == organization)
                    & (User.is_org == True)
                    & (UserOrganization.user == user.id)
                )
                .first()
            )
            if org is None:
                raise HTTPException(403, "Organization membership required")
            context.append(org.id)
        state = (
            _decode(cursor, context) if cursor else {"v": 1, "context": context, "caps": [0, 0, 0]}
        )
        branches, params = [], []
        for rank, (table, actor, join) in enumerate(
            (
                ("repository", "CAST(NULL AS INTEGER)", ""),
                ("commit", "e.author_id", "JOIN repository r ON r.id=e.repository_id"),
                ("repositorylike", "e.user_id", "JOIN repository r ON r.id=e.repository_id"),
            )
        ):
            if (event_type == "repository" and rank == 2) or (event_type == "like" and rank != 2):
                continue
            source = "r" if rank == 0 else "e"
            epoch = _epoch(f"{source}.created_at")
            personal = f"({own} OR {actor} = {p})"
            self_interest = f"r.owner_id = {p}" if rank == 0 else f"{actor} = {p}"
            self_params = [user.id]
            if rank == 0:
                interest = f"r.owner_id IN ({followed})"
                interest_params = [user.id]
            else:
                interest = f"({actor} IN ({followed_people}) OR r.owner_id IN ({followed_orgs}))"
                interest_params = [user.id, user.id]
            member_orgs = f"({own} AND r.owner_id <> {p})"
            where = [visible]
            branch_params = list(visible_params)
            if scope == "personal":
                branch_params += own_params + [user.id]
            if scope == "all":
                branch_params += self_params + own_params + [user.id] + interest_params
            if scope == "following":
                branch_params += interest_params
            if scope == "self":
                branch_params += self_params
            if scope == "organization":
                branch_params += [org.id]
            scope_filter = {
                "personal": personal,
                "following": interest,
                "all": f"({self_interest} OR {member_orgs} OR {interest})",
                "self": self_interest,
                "organization": f"r.owner_id = {p}",
            }
            where += [scope_filter[scope]]
            if rank == 1:
                where += ["e.branch = 'main'"]
            if repo_type != "all":
                where += [f"r.repo_type = {p}"]
                branch_params += [repo_type]
            if not cursor:
                # Opaque cursors must not expose maximum IDs from invisible activity.
                state["caps"][rank] = (
                    database.execute_sql(
                        f'SELECT MAX({source}.id) FROM "{table}" {source} {join} WHERE '
                        + " AND ".join(where),
                        branch_params,
                    ).fetchone()[0]
                    or 0
                )
            where += [f"{source}.id <= {p}"]
            branch_params += [state["caps"][rank]]
            if cursor:
                stamp, last_rank, row_id = state["last"]
                where += [
                    f"({epoch} < {p} OR ({epoch} = {p} AND ({rank} < {p} OR ({rank} = {p} AND {source}.id < {p}))))"
                ]
                branch_params += [stamp, stamp, last_rank, last_rank, row_id]
            message = "e.message" if rank == 1 else "NULL"
            sha = "e.commit_id" if rank == 1 else "NULL"
            projection = (
                f'SELECT {source}.id AS event_id,{rank} AS rank,{source}.created_at AS created_at,{epoch} AS stamp,r.id AS repo_id,{actor} AS actor_id,{sha} AS sha,{message} AS message FROM "{table}" {source} {join} WHERE '
                + " AND ".join(where)
            )
            # The global top N must be contained in the top N of each source.
            branches += [
                f"SELECT * FROM ({projection} ORDER BY stamp DESC,event_id DESC LIMIT {p}) source_{rank}"
            ]
            params += branch_params + [limit + 1]
        rows = database.execute_sql(
            "SELECT * FROM ("
            + " UNION ALL ".join(branches)
            + f") activity ORDER BY stamp DESC,rank DESC,event_id DESC LIMIT {p}",
            params + [limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        repos = {
            repo.id: repo
            for repo in Repository.select(
                Repository.id,
                Repository.full_id,
                Repository.repo_type,
                Repository.private,
                Repository.owner,
                Repository.created_at,
                Repository.downloads,
                Repository.likes_count,
            ).where(Repository.id.in_([row[4] for row in rows]))
        }
        heads = _latest_main_commits(list(repos))
        profiles = _profiles(
            {row[5] for row in rows if row[5] is not None}
            | {repo.owner_id for repo in repos.values()}
        )
        items = []
        kinds = ("repo_created", "commit", "like")
        for event_id, rank, created_at, _, repo_id, actor_id, sha, message in rows:
            repo = repos[repo_id]
            namespace = profiles[repo.owner_id]
            items.append(
                {
                    "id": f"{kinds[rank]}:{event_id}",
                    "kind": kinds[rank],
                    "created_at": _utc(created_at),
                    "actor": profiles.get(actor_id),
                    "namespace": {"username": namespace["username"], "is_org": namespace["is_org"]},
                    "repository": {
                        "id": repo.full_id,
                        "type": repo.repo_type,
                        "private": repo.private,
                        "lastModified": _utc(
                            heads[repo.id][1] if repo.id in heads else repo.created_at
                        ),
                        "downloads": repo.downloads,
                        "likes": repo.likes_count,
                    },
                    "commit": {"sha": sha, "message": message[:1000]} if rank == 1 else None,
                }
            )
        next_cursor = (
            _encode({**state, "last": [rows[-1][3], rows[-1][1], rows[-1][0]]})
            if has_more
            else None
        )
        return {"items": items, "has_more": has_more, "next_cursor": next_cursor}


@router.get("/workspace/feed")
def workspace_feed(
    response: Response,
    scope: Literal["all", "personal", "following", "self", "organization"] = "all",
    repo_type: Literal["all", "model", "dataset", "space"] = "all",
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    organization: str | None = None,
    event_type: Literal["all", "repository", "like"] = "all",
    user: User = Depends(get_current_user),
):
    _individual(user)
    _no_store(response)
    return _feed(user, scope, repo_type, limit, cursor, organization, event_type)
