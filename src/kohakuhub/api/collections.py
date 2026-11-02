"""Collections: curated lists of repositories, compatible with Hugging Face's ``/api/collections``.

What the wire shape follows (read from HF's OpenAPI spec, ``huggingface_hub`` and the live API, 2026-11-02):

* a collection is ``{owner}/{title-slug}-{24 hex}``; ``{owner}/{title-slug}`` is the short form (and the share URL);
  the same id behind another title answers ``307`` to the canonical slug
* title 1-60 characters, description at most 150, an item's note at most 500 (plain text, returned as ``{text, html}``)
* ``theme`` is one of six colours, drawn at random at creation
* items are models, datasets, Spaces and other collections (a reference: shown as a card, never expanded)
* ``PATCH`` of a collection answers ``{"data": collection}``; creating a title whose slug the owner already has is ``409``
  with ``{"error", "slug"}``; the list truncates ``items`` to 4 and pages with an absolute ``Link: <...>; rel="next"``
* errors are ``{"error": message}``
"""

import base64
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from peewee import JOIN, fn

from kohakuhub.auth.dependencies import get_current_user, get_optional_user
from kohakuhub.auth.permissions import repository_read_predicate
from kohakuhub.config import cfg
from kohakuhub.db import (
    Collection,
    CollectionItem,
    CollectionUpvote,
    Repository,
    RepositoryLike,
    User,
    UserOrganization,
    db,
)
from kohakuhub.logger import get_logger
from kohakuhub.repository_discovery import serialize_items

logger = get_logger("COLLECTIONS")
router = APIRouter()

THEMES = ("orange", "blue", "green", "purple", "pink", "indigo")
REPO_TYPES = ("model", "dataset", "space")
UNSUPPORTED_TYPES = ("paper", "bucket")
TITLE_MAX, DESCRIPTION_MAX, NOTE_MAX = 60, 150, 500
LIST_PREVIEW_ITEMS = 4
# Not documented by Hugging Face (the largest public collection seen has 588 items, the most collections of one owner 138)
MAX_ITEMS_PER_COLLECTION = 1000
MAX_COLLECTIONS_PER_OWNER = 500
HEX24 = re.compile(r"^[0-9a-fA-F]{24}$")
SLUG_WITH_ID = re.compile(r"^(.*)-([0-9a-fA-F]{24})$")


# ---------------------------------------------------------------- small helpers

def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": message, **extra}, headers={"X-Error-Message": message[:300]}
    )


def _not_found() -> JSONResponse:
    return _err(404, "Collection not found")


def new_oid() -> str:
    """24 hex characters like a MongoDB ObjectId: 4 bytes of time, 8 random bytes."""
    return f"{int(time.time()):08x}" + secrets.token_hex(8)


def title_slug(title: str) -> str:
    """'💧 LFM3' -> 'lfm3': lower case, anything that is not a letter or digit becomes a hyphen."""
    slug = re.sub(r"[\W_]+", "-", title.lower()).strip("-")[:60].strip("-")
    return slug or "collection"


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _public_url(request: Request) -> str:
    return (cfg.app.base_url or str(request.base_url)).rstrip("/")


def full_slug(col: Collection) -> str:
    return f"{col.owner.username}/{col.slug}-{col.oid}"


def share_url(col: Collection, request: Request) -> str:
    return f"{_public_url(request)}/collections/{col.owner.username}/{col.slug}"


def _object_id(number: int) -> str:
    return f"{number:024x}"


def serialize_owner(owner: User) -> dict:
    kind = "org" if owner.is_org else "user"
    avatar = f"/api/{'organizations' if owner.is_org else 'users'}/{owner.username}/avatar"
    out = {
        "_id": _object_id(owner.id), "avatarUrl": avatar, "fullname": owner.full_name or owner.username,
        "name": owner.username, "isHf": False, "isHfAdmin": False, "isMod": False, "type": kind,
    }
    if not owner.is_org:
        out["isPro"] = False   # required for a user by the spec, not allowed for an organization
    return out


def render_note(text: str) -> dict:
    """Plain text with the web addresses turned into links, as the Hub does ({text, html})."""
    safe = escape(text)
    html = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" rel="noopener nofollow">\1</a>', safe)
    return {"text": text, "html": html.replace("\n", "<br>")}


# ---------------------------------------------------------------- who may see and change what

def _is_member(user: User | None, owner: User, writer_only: bool) -> bool:
    if user is None:
        return False
    if owner.id == user.id:
        return True
    if not owner.is_org:
        return False
    roles = ("member", "admin", "super-admin") if writer_only else ("visitor", "member", "admin", "super-admin")
    return UserOrganization.select().where(
        (UserOrganization.user == user) & (UserOrganization.organization == owner) & (UserOrganization.role.in_(roles))
    ).exists()


def can_read(col: Collection, user: User | None) -> bool:
    return (not col.private) or _is_member(user, col.owner, writer_only=False)


def can_write(col: Collection, user: User | None) -> bool:
    return _is_member(user, col.owner, writer_only=True)


def _visible_collections(user: User | None):
    """SQL predicate: public ones, plus private ones of the caller and of the organizations they belong to."""
    if user is None:
        return Collection.private == False  # noqa: E712
    mine = Collection.owner.in_(
        UserOrganization.select(UserOrganization.organization).where(UserOrganization.user == user)
    ) | (Collection.owner == user)
    return (Collection.private == False) | mine  # noqa: E712


# ---------------------------------------------------------------- finding a collection

def resolve(namespace: str, slug: str) -> tuple[Collection | None, bool]:
    """-> (collection, canonical): ``canonical`` is False when the path was the short form or another title."""
    match = SLUG_WITH_ID.match(slug)
    if match:
        col = (
            Collection.select(Collection, User)
            .join(User, on=(Collection.owner == User.id))
            .where((Collection.oid == match.group(2).lower()) & (User.username == namespace))
            .first()
        )
        return col, bool(col) and col.slug == match.group(1)
    col = (
        Collection.select(Collection, User)
        .join(User, on=(Collection.owner == User.id))
        .where((Collection.slug == slug) & (User.username == namespace))
        .first()
    )
    return col, False


def _load(namespace: str, slug: str, user: User | None, request: Request, write: bool = False):
    """-> (collection, None) or (None, response); an unreadable collection is simply not found."""
    col, canonical = resolve(namespace, slug)
    if col is None or not can_read(col, user):
        return None, _not_found()
    if SLUG_WITH_ID.match(slug) and not canonical:
        target = f"{cfg.app.api_base}/collections/{full_slug(col)}"
        suffix = request.url.path.split(slug, 1)[1] if slug in request.url.path else ""
        return None, RedirectResponse(url=target + suffix, status_code=307)
    if write and not can_write(col, user):
        return None, _err(403, "You don't have permission to edit this collection")
    return col, None


# ---------------------------------------------------------------- serialization

def _visible_items(col: Collection, user: User | None):
    """The items this caller may see: a repository they can read, a collection they can read."""
    rows = list(
        CollectionItem.select(CollectionItem, Repository, Collection)
        .join(Repository, JOIN.LEFT_OUTER, on=(CollectionItem.repository == Repository.id))
        .switch(CollectionItem)
        .join(Collection, JOIN.LEFT_OUTER, on=(CollectionItem.child == Collection.id))
        .where(CollectionItem.collection == col)
        .order_by(CollectionItem.position, CollectionItem.id)
    )
    visible = []
    readable_ids = None
    if any(r.repository_id for r in rows):
        ids = [r.repository_id for r in rows if r.repository_id]
        readable_ids = {
            rid
            for (rid,) in Repository.select(Repository.id)
            .where(Repository.id.in_(ids) & repository_read_predicate(user))
            .tuples()
        }
    for row in rows:
        if row.repository_id:
            if row.repository_id in readable_ids:
                visible.append(row)
        else:
            child = Collection.get_by_id(row.child_id)
            if can_read(child, user):
                visible.append(row)
    return visible


def _serialize_item(row: CollectionItem, info: dict, user: User | None, request: Request, liked: set) -> dict:
    out = {"_id": row.oid, "position": row.position, "type": row.item_type}
    if row.note:
        out["note"] = render_note(row.note)
    if row.repository_id:
        repo = Repository.get_by_id(row.repository_id)
        data = info.get(repo.id, {})
        meta = data.get("metadata") or {}
        modified = data.get("lastModified")
        out.update({
            "author": repo.namespace, "id": repo.full_id, "repoType": repo.repo_type, "private": repo.private,
            "likes": repo.likes_count, "lastModified": _iso(datetime.fromisoformat(modified) if modified else repo.created_at),
            "isLikedByUser": repo.id in liked,
        })
        # The item variants of the spec are closed, and they differ: a model and a dataset have downloads and gated (a model
        # also availableInferenceProviders and maybe a pipeline_tag); a Space has no downloads or gated but needs its
        # colours, emoji, title, createdAt, pinned, featured, disabled, visibility, tags and a runtime. Nothing has `tags`
        # except a Space.
        if repo.repo_type == "space":
            out.update({
                "colorFrom": meta.get("colorFrom", "gray"), "colorTo": meta.get("colorTo", "gray"),
                "emoji": meta.get("emoji", "🧀"), "title": meta.get("title", repo.name), "createdAt": _iso(repo.created_at),
                "pinned": False, "featured": False, "disabled": False, "visibility": "private" if repo.private else "public",
                "tags": list(meta.get("tags", [])),
                "runtime": {"stage": "STOPPED", "hardware": {"current": None, "requested": None}, "replicas": {"current": None, "requested": 1}},   # CheeseCave does not run Spaces
            })
            if meta.get("sdk") in ("gradio", "docker", "static", "streamlit"):
                out["sdk"] = meta["sdk"]
        else:
            out.update({"downloads": repo.downloads, "gated": False})
            if repo.repo_type == "model":
                out["availableInferenceProviders"] = []
                if meta.get("pipeline_tag"):
                    out["pipeline_tag"] = meta["pipeline_tag"]
    else:
        child = Collection.get_by_id(row.child_id)
        count = len(_visible_items(child, user))
        out.update({
            "id": full_slug(child), "slug": full_slug(child), "title": child.title, "owner": serialize_owner(child.owner),
            "lastUpdated": _iso(child.updated_at), "numberItems": count, "theme": child.theme,
            "upvotes": child.upvotes_count, "isUpvotedByUser": _upvoted(child, user), "shareUrl": share_url(child, request),
            **({"description": child.description} if child.description else {}),
        })
    return out


def _upvoted(col: Collection, user: User | None) -> bool:
    return bool(user) and CollectionUpvote.select().where(
        (CollectionUpvote.collection == col) & (CollectionUpvote.user == user)
    ).exists()


def serialize(col: Collection, user: User | None, request: Request, limit_items: int | None = None) -> dict:
    """``limit_items`` is set for a list entry, which (like Hugging Face's) has no ``position`` and no ``shareUrl``:
    ``huggingface_hub`` passes ``position=`` itself when it builds a Collection from a list."""
    rows = _visible_items(col, user)
    if limit_items is not None:
        rows = rows[:limit_items]
    repo_ids = [r.repository_id for r in rows if r.repository_id]
    info = {}
    if repo_ids:
        repos = list(Repository.select().where(Repository.id.in_(repo_ids)))
        info = {repo.id: data for repo, data in zip(repos, serialize_items(repos))}   # full_id alone is not unique across types
    liked = set()
    if user and repo_ids:
        liked = {
            rid for (rid,) in RepositoryLike.select(RepositoryLike.repository)
            .where((RepositoryLike.user == user) & RepositoryLike.repository.in_(repo_ids)).tuples()
        }
    out = {
        "slug": full_slug(col), "title": col.title, "owner": serialize_owner(col.owner),
        "items": [_serialize_item(r, info, user, request, liked) for r in rows],
        "lastUpdated": _iso(col.updated_at), "private": col.private, "theme": col.theme,
        "upvotes": col.upvotes_count, "isUpvotedByUser": _upvoted(col, user),
        "gating": False, **({"description": col.description} if col.description else {}),
    }
    if limit_items is None:
        out.update(position=col.position, shareUrl=share_url(col, request))
    return out


# ---------------------------------------------------------------- create, read, update, delete

def _check_text(title=None, description=None):
    if title is not None and not (1 <= len(title) <= TITLE_MAX):
        return _err(400, f"title must be 1 to {TITLE_MAX} characters")
    if description is not None and len(description) > DESCRIPTION_MAX:
        return _err(400, f"description must be at most {DESCRIPTION_MAX} characters")
    return None


class _Rollback(Exception):
    """Carries the response of a failure out of a transaction, which this way is rolled back."""

    def __init__(self, response):
        self.response = response


def _body(request_json) -> dict:
    return request_json if isinstance(request_json, dict) else {}


@router.post("/collections")
async def create_collection(request: Request, user: User = Depends(get_current_user)):
    body = _body(await request.json())
    title, namespace = body.get("title"), body.get("namespace") or user.username
    if not isinstance(title, str):
        return _err(400, "title is required")
    if (bad := _check_text(title, body.get("description"))) is not None:
        return bad
    owner = User.get_or_none(User.username == namespace)
    if owner is None or not _is_member(user, owner, writer_only=True):
        return _err(403, f"You can't create collections for '{namespace}'")
    slug = title_slug(title)
    existing = Collection.get_or_none((Collection.owner == owner) & (Collection.slug == slug))
    if existing:
        return _err(409, "The collection already exists", slug=full_slug(existing))
    if Collection.select().where(Collection.owner == owner).count() >= MAX_COLLECTIONS_PER_OWNER:
        return _err(400, f"An owner can have at most {MAX_COLLECTIONS_PER_OWNER} collections")
    try:
        with db.atomic():
            col = Collection.create(
                oid=new_oid(), owner=owner, title=title, slug=slug, description=body.get("description"),
                theme=secrets.choice(THEMES), private=bool(body.get("private", False)),
                position=Collection.select(fn.COALESCE(fn.MAX(Collection.position) + 1, 0)).where(Collection.owner == owner).scalar(),
            )
            if body.get("item"):
                failure = _add_item(col, body["item"], None, user)
                if failure is not None:
                    raise _Rollback(failure)
    except _Rollback as stop:
        return stop.response
    col = Collection.select(Collection, User).join(User, on=(Collection.owner == User.id)).where(Collection.id == col.id).get()
    return serialize(col, user, request)


@router.get("/collections/{namespace}/{slug}")
async def get_collection(namespace: str, slug: str, request: Request, user: User | None = Depends(get_optional_user)):
    col, failure = _load(namespace, slug, user, request)
    if failure is not None:
        return failure
    return JSONResponse(serialize(col, user, request), headers={"ETag": f'W/"{col.oid}-{int(col.updated_at.timestamp())}"'})


@router.patch("/collections/{namespace}/{slug}")
async def update_collection(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    body = _body(await request.json())
    if (bad := _check_text(body.get("title"), body.get("description"))) is not None:
        return bad
    if "theme" in body and body["theme"] not in THEMES:
        return _err(400, f"theme must be one of {', '.join(THEMES)}")
    changes = {}
    if "title" in body:
        new_slug = title_slug(body["title"])
        clash = Collection.get_or_none((Collection.owner == col.owner) & (Collection.slug == new_slug) & (Collection.id != col.id))
        if clash:
            return _err(409, "The collection already exists", slug=full_slug(clash))
        changes.update(title=body["title"], slug=new_slug)
    for key in ("description", "theme", "private", "position"):
        if key in body:
            changes[key] = body[key]
    changes["updated_at"] = datetime.now(timezone.utc)
    Collection.update(**changes).where(Collection.id == col.id).execute()
    col = Collection.select(Collection, User).join(User, on=(Collection.owner == User.id)).where(Collection.id == col.id).get()
    return {"success": True, "data": serialize(col, user, request)}


@router.delete("/collections/{namespace}/{slug}")
async def delete_collection(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    col.delete_instance()
    return Response(status_code=204)


# ---------------------------------------------------------------- items

def _find_target(item: dict, user: User | None):
    """-> (type, repository, child, None) or (..., failure response)."""
    kind, ident = (item or {}).get("type"), (item or {}).get("id")
    if kind in UNSUPPORTED_TYPES:
        return None, None, None, _err(501, f"{kind} items are not supported by CheeseCave", )
    if kind not in REPO_TYPES + ("collection",) or not isinstance(ident, str) or not ident:
        return None, None, None, _err(400, "item must be {type: model|dataset|space|collection, id: string}")
    if kind == "collection":
        ns, _, rest = ident.partition("/")
        child, _ = resolve(ns, rest)
        if child is None or not can_read(child, user):
            return None, None, None, _err(404, "Collection not found")
        return kind, None, child, None
    ns, _, name = ident.partition("/")
    repo = Repository.get_or_none((Repository.repo_type == kind) & (Repository.namespace == ns) & (Repository.name == name))
    readable = repo is not None and Repository.select().where((Repository.id == repo.id) & repository_read_predicate(user)).exists()
    if not readable:
        return None, None, None, _err(404, "Repository not found")
    return kind, repo, None, None


def _add_item(col: Collection, item: dict, note: str | None, user: User):
    if note is not None and len(note) > NOTE_MAX:
        return _err(400, f"note must be at most {NOTE_MAX} characters")
    kind, repo, child, failure = _find_target(item, user)
    if failure is not None:
        return failure
    if child is not None and child.id == col.id:
        return _err(400, "A collection can't contain itself")
    if CollectionItem.select().where(CollectionItem.collection == col).count() >= MAX_ITEMS_PER_COLLECTION:
        return _err(400, f"A collection can have at most {MAX_ITEMS_PER_COLLECTION} items")
    duplicate = CollectionItem.select().where(
        (CollectionItem.collection == col)
        & ((CollectionItem.repository == repo) if repo is not None else (CollectionItem.child == child))
    ).exists()
    if duplicate:
        return _err(409, "Item already exists in the collection")
    top = CollectionItem.select(fn.MAX(CollectionItem.position)).where(CollectionItem.collection == col).scalar()
    CollectionItem.create(
        oid=new_oid(), collection=col, item_type=kind, repository=repo, child=child, note=note or None,
        position=0 if top is None else top + 1,
    )
    Collection.update(updated_at=datetime.now(timezone.utc)).where(Collection.id == col.id).execute()
    return None


@router.post("/collections/{namespace}/{slug}/items")
async def add_item(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    body = _body(await request.json())
    with db.atomic():
        failure = _add_item(col, body.get("item"), body.get("note"), user)
    if failure is not None:
        return failure
    return serialize(Collection.select(Collection, User).join(User, on=(Collection.owner == User.id)).where(Collection.id == col.id).get(), user, request)


def _move(col: Collection, item: CollectionItem, position: int):
    """Put ``item`` at ``position`` and renumber the others around it, in order."""
    others = [r for r in CollectionItem.select().where(CollectionItem.collection == col).order_by(CollectionItem.position, CollectionItem.id) if r.id != item.id]
    others.insert(max(0, min(position, len(others))), item)
    for index, row in enumerate(others):
        if row.position != index:
            CollectionItem.update(position=index).where(CollectionItem.id == row.id).execute()


def _update_item(col: Collection, item: CollectionItem, data: dict):
    note = data.get("note")
    if note is not None and len(note) > NOTE_MAX:
        return _err(400, f"note must be at most {NOTE_MAX} characters")
    if note is not None:
        CollectionItem.update(note=note or None).where(CollectionItem.id == item.id).execute()
    if "position" in data:
        if not isinstance(data["position"], int) or data["position"] < 0:
            return _err(400, "position must be an integer of at least 0")
        _move(col, item, data["position"])
    # `gallery` (image addresses of an item) is accepted and not stored yet
    Collection.update(updated_at=datetime.now(timezone.utc)).where(Collection.id == col.id).execute()
    return None


@router.patch("/collections/{namespace}/{slug}/items/{item_id}")
async def update_item(namespace: str, slug: str, item_id: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    item = CollectionItem.get_or_none((CollectionItem.collection == col) & (CollectionItem.oid == item_id.lower()))
    if item is None:
        return _err(404, "Item not found")
    with db.atomic():
        failure = _update_item(col, item, _body(await request.json()))
    return failure or JSONResponse(content={})


@router.post("/collections/{namespace}/{slug}/items/batch")
async def batch_items(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    actions = await request.json()
    if not isinstance(actions, list):
        return _err(400, "body must be a list of {action, _id, data}")
    try:
        with db.atomic():
            for action in actions:
                if not isinstance(action, dict) or action.get("action") != "update":
                    raise _Rollback(_err(400, "only the action 'update' exists"))
                item = CollectionItem.get_or_none(
                    (CollectionItem.collection == col) & (CollectionItem.oid == str(action.get("_id", "")).lower())
                )
                if item is None:
                    raise _Rollback(_err(404, "Item not found"))
                failure = _update_item(col, item, _body(action.get("data")))
                if failure is not None:
                    raise _Rollback(failure)
    except _Rollback as stop:
        return stop.response
    return JSONResponse(content={})


@router.delete("/collections/{namespace}/{slug}/items/{item_id}")
async def delete_item(namespace: str, slug: str, item_id: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    item = CollectionItem.get_or_none((CollectionItem.collection == col) & (CollectionItem.oid == item_id.lower()))
    if item is None:
        return _err(404, "Item not found")
    item.delete_instance()   # the other positions stay as they are: gaps are normal
    Collection.update(updated_at=datetime.now(timezone.utc)).where(Collection.id == col.id).execute()
    return Response(status_code=204)


# ---------------------------------------------------------------- upvotes (the web page; no Hugging Face client method)

@router.post("/collections/{namespace}/{slug}/upvote")
async def upvote(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request)
    if failure is not None:
        return failure
    with db.atomic():
        if not _upvoted(col, user):
            CollectionUpvote.create(collection=col, user=user)
            Collection.update(upvotes_count=Collection.upvotes_count + 1).where(Collection.id == col.id).execute()
    return {"success": True, "upvotes": Collection.get_by_id(col.id).upvotes_count}


@router.delete("/collections/{namespace}/{slug}/upvote")
async def remove_upvote(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request)
    if failure is not None:
        return failure
    with db.atomic():
        if CollectionUpvote.delete().where((CollectionUpvote.collection == col) & (CollectionUpvote.user == user)).execute():
            Collection.update(upvotes_count=Collection.upvotes_count - 1).where(Collection.id == col.id).execute()
    return {"success": True, "upvotes": Collection.get_by_id(col.id).upvotes_count}


# ---------------------------------------------------------------- listing

def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def _offset(cursor: str | None) -> int | None:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["o"])
    except Exception:
        return None


@router.get("/collections")
async def list_collections(request: Request, user: User | None = Depends(get_optional_user)):
    q = request.query_params
    owners, items = q.getlist("owner"), q.getlist("item")
    sort, expand = q.get("sort", "trending"), q.get("expand", "true").lower() != "false"
    # "position" is a CheeseCave extension: the owner's own order, for the profile and the owner's page (a Hugging Face
    # client never sends it; Hugging Face's own list answers 400 to it)
    if sort not in ("trending", "upvotes", "lastModified", "position"):
        return _err(400, 'sort must be one of "upvotes", "lastModified", "trending"')
    try:
        limit = int(q.get("limit", 10))
    except ValueError:
        return _err(400, "limit must be a number")
    if limit < 1 or limit > 10000:
        return _err(400, "limit must be between 1 and 10000")
    if limit > 100 and not (not expand and owners):
        return _err(400, "Limit must be less than or equal to 100 unless expand is false and owner is set")
    for ref in items:
        if not re.match(r"^(spaces|models|datasets|papers)/.+", ref):
            return _err(400, "item must look like models/<id>, datasets/<id>, spaces/<id> or papers/<id>")
    offset = _offset(q.get("cursor"))
    if offset is None:
        return _err(400, "invalid cursor")

    query = (
        Collection.select(Collection, User).join(User, on=(Collection.owner == User.id)).where(_visible_collections(user))
    )
    if owners:
        query = query.where(User.username.in_(owners))
    if q.get("q"):
        term = f"%{q['q']}%"   # ilike: case-insensitive on SQLite (plain like is a case-sensitive GLOB there) and PostgreSQL
        query = query.where(Collection.title.ilike(term) | Collection.description.ilike(term))
    for ref in items:
        kind, _, ident = ref.partition("/")
        if kind == "papers":
            query = query.where(Collection.id == -1)   # no paper catalogue here: nothing contains one
            continue
        ns, _, name = ident.partition("/")
        repo_type = kind[:-1]
        hit = (
            CollectionItem.select(CollectionItem.collection)
            .join(Repository, on=(CollectionItem.repository == Repository.id))
            .where(
                (Repository.repo_type == repo_type) & (Repository.namespace == ns) & (Repository.name == name)
                & repository_read_predicate(user)   # a repository the caller cannot read matches nothing, as if it did not exist
            )
        )
        query = query.where(Collection.id.in_(hit))
    if sort == "position":
        query = query.order_by(User.username, Collection.position, Collection.id)
    elif sort == "upvotes":
        query = query.order_by(Collection.upvotes_count.desc(), Collection.updated_at.desc(), Collection.id.desc())
    elif sort == "lastModified":
        query = query.order_by(Collection.updated_at.desc(), Collection.id.desc())
    else:   # trending: upvotes of the last 7 days, then the latest change
        recent = (
            CollectionUpvote.select(CollectionUpvote.collection, fn.COUNT(CollectionUpvote.id).alias("recent"))
            .where(CollectionUpvote.created_at >= datetime.now(timezone.utc) - timedelta(days=7))
            .group_by(CollectionUpvote.collection).alias("r")
        )
        query = query.join(recent, JOIN.LEFT_OUTER, on=(Collection.id == recent.c.collection_id)).order_by(
            fn.COALESCE(recent.c.recent, 0).desc(), Collection.updated_at.desc(), Collection.id.desc()
        )
    rows = list(query.offset(offset).limit(limit + 1))
    more, rows = len(rows) > limit, rows[:limit]
    if not expand and owners:
        body = [{"name": full_slug(c), "private": c.private, "title": c.title, "upvotes": c.upvotes_count} for c in rows]
    else:
        body = [serialize(c, user, request, limit_items=LIST_PREVIEW_ITEMS) for c in rows]
    headers = {}
    if more:
        params = [(k, v) for k, v in q.multi_items() if k != "cursor"] + [("cursor", _cursor(offset + limit))]
        headers["Link"] = f'<{_public_url(request)}{request.url.path}?{urlencode(params)}>; rel="next"'
    return JSONResponse(body, headers=headers)
