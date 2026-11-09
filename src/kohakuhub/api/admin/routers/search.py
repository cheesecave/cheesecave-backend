"""Global search endpoints for admin API."""

from fastapi import APIRouter, Depends, HTTPException, Query

from peewee import fn

from kohakuhub.db import Commit, Repository, User
from kohakuhub.logger import get_logger
from kohakuhub.api.admin.utils import verify_admin_token

logger = get_logger("ADMIN")
router = APIRouter()


@router.get("/search")
async def global_search(
    q: str,
    types: list[str] = Query(default=["users", "repos", "commits"]),
    limit: int = 20,
    _admin: bool = Depends(verify_admin_token),
):
    """Global search across users, organizations, repositories, and commits.

    Args:
        q: Search query string
        types: Types to search (users, orgs, repos, commits)
        limit: Maximum results per type
        _admin: Admin authentication (dependency)

    Returns:
        Grouped search results by type
    """
    results = {"users": [], "organizations": [], "repositories": [], "commits": []}
    text = q.strip().lower()

    # Users and organizations: ranked like the pickers (id, exact, prefix, contains), organizations only when asked
    if "users" in types:
        results["users"] = _accounts(text, ["user"], limit)
    if "orgs" in types:
        results["organizations"] = _accounts(text, ["org"], limit)

    # Search repositories
    if "repos" in types or "repositories" in types:
        repos_query = (
            Repository.select()
            .where((Repository.full_id.contains(q)) | (Repository.name.contains(q)))
            .limit(limit)
        )

        results["repositories"] = [
            {
                "id": r.id,
                "full_id": r.full_id,
                "repo_type": r.repo_type,
                "namespace": r.namespace,
                "name": r.name,
                "private": r.private,
                "owner_username": r.owner.username if r.owner else "unknown",
            }
            for r in repos_query
        ]

    # Search commits
    if "commits" in types:
        commits_query = (
            Commit.select()
            .where((Commit.message.contains(q)) | (Commit.username.contains(q)))
            .limit(limit)
        )

        results["commits"] = [
            {
                "id": c.id,
                "commit_id": c.commit_id,
                "message": c.message,
                "username": c.username,
                "repo_full_id": c.repository.full_id if c.repository else None,
                "repo_type": c.repo_type,
                "branch": c.branch,
                "created_at": c.created_at.isoformat(),
            }
            for c in commits_query
        ]

    return {
        "query": q,
        "results": results,
        "result_counts": {
            "users": len(results["users"]),
            "organizations": len(results["organizations"]),
            "repositories": len(results["repositories"]),
            "commits": len(results["commits"]),
        },
    }


# ---------------------------------------------------------------- lookup: the typeahead behind every picker
_LOOKUP_KINDS = ("user", "org", "repo")
_CANDIDATE_CAP = 500  # SQL pre-filter; the exact match and the ranking are done on these rows
# Rows the database matched but the string comparison cannot confirm (case folding differs between engines): still listed,
# after every confirmed match.
_UNCONFIRMED_USER = (7, "username")
_UNCONFIRMED_REPO = (7, "full_id")


def _rank_name(q: str, *fields: tuple[str, str | None]) -> tuple[int, str] | None:
    """(rank, field) of the best way ``q`` matches one of ``fields`` (name, value), or None.

    Exact beats prefix beats contains, and the first field wins a tie. ``q`` is lower-cased; LIKE wildcards in what was
    typed are literal because the check is a plain string comparison.
    """
    best: tuple[int, str] | None = None
    for order, (name, value) in enumerate(fields):
        if not value:
            continue
        v = value.lower()
        if v == q:
            rank = 1 + order
        elif v.startswith(q):
            rank = 3 + order
        elif q in v:
            rank = 5 + order
        else:
            continue
        if best is None or rank < best[0]:
            best = (rank, name)
    return best


@router.get("/lookup")
async def lookup_entities(
    q: str = "",
    kinds: list[str] = Query(default=["user", "org"]),
    limit: int = Query(default=8, ge=1, le=20),
    repo_type: str | None = None,
    _admin: bool = Depends(verify_admin_token),
):
    """Suggestions for a picker: users, organizations and repositories by name, email, id or full id.

    One typed word matches a username, an email address, a numeric id (``12`` or ``#12``) or a repository full id.
    Best matches first: id, exact, prefix, contains. No quota or usage is computed, so it is cheap enough for a keystroke.

    Args:
        q: What was typed (empty lists the first rows by name)
        kinds: Any of ``user``, ``org``, ``repo``
        limit: Maximum suggestions in total
        repo_type: Restrict ``repo`` suggestions to model / dataset / space
    """
    bad = [k for k in kinds if k not in _LOOKUP_KINDS]
    if bad:
        raise HTTPException(400, detail={"error": f"Unknown kind: {', '.join(bad)}"})

    text = q.strip().lower()
    ranked = _ranked_accounts(text, [k for k in kinds if k in ("user", "org")], limit)

    if "repo" in kinds:
        query = Repository.select()
        if repo_type:
            query = query.where(Repository.repo_type == repo_type)
        if text:
            query = query.where((Repository.full_id.contains(text)) | (Repository.name.contains(text)))
        for r in query.order_by(Repository.full_id).limit(_CANDIDATE_CAP if text else limit):
            rank, field = _rank_name(text, ("full_id", r.full_id), ("name", r.name)) or _UNCONFIRMED_REPO
            ranked.append((rank + 0.5, r.full_id.lower(), {
                "kind": "repo",
                "id": r.id,
                "full_id": r.full_id,
                "repo_type": r.repo_type,
                "namespace": r.namespace,
                "name": r.name,
                "private": r.private,
                "matched": field,
            }))

    ranked.sort(key=lambda item: (item[0], item[1]))
    seen, results = set(), []
    for _rank, _key, item in ranked:
        key = (item["kind"], item["id"])
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
        if len(results) >= limit:
            break
    return {"query": q, "results": results}


def _ranked_accounts(text: str, kinds: list[str], limit: int) -> list[tuple[float, str, dict]]:
    """Users and/or organizations matching ``text`` (lower-cased), as (rank, sort key, entity) triples.

    Rank 0 is the row whose id was typed (``12`` or ``#12``), then exact, prefix and contains matches of the username and the
    email. Only the columns an entity shows are read: the avatar is a blob, so it is asked for as a flag.
    """
    if not kinds:
        return []
    ranked: list[tuple[float, str, dict]] = []
    base = User.select(
        User.id,
        User.username,
        User.email,
        User.email_verified,
        User.is_org,
        User.is_active,
        User.avatar.is_null(False).alias("has_avatar"),
    ).where(User.is_org << [k == "org" for k in kinds])
    digits = text.lstrip("#")
    if digits.isdigit():
        for u in base.where(User.id == int(digits)):
            ranked.append((0, u.username.lower(), _entity(u, "id")))
    query = base
    if text:
        query = base.where((User.username.contains(text)) | (User.email.contains(text)))
    for u in query.order_by(fn.Lower(User.username)).limit(_CANDIDATE_CAP if text else limit):
        rank, field = _rank_name(text, ("username", u.username), ("email", u.email)) or _UNCONFIRMED_USER
        ranked.append((rank, u.username.lower(), _entity(u, field)))
    return ranked


def _accounts(text: str, kinds: list[str], limit: int) -> list[dict]:
    """The best ``limit`` users or organizations for ``text``, without duplicates."""
    ranked = _ranked_accounts(text, kinds, limit)
    ranked.sort(key=lambda item: (item[0], item[1]))
    seen: set[int] = set()
    out: list[dict] = []
    for _rank, _key, item in ranked:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _entity(u: User, matched: str) -> dict:
    return {
        "kind": "org" if u.is_org else "user",
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "email_verified": u.email_verified,
        "is_org": u.is_org,
        "is_active": u.is_active,
        "has_avatar": bool(u.has_avatar),
        "matched": matched,
    }
