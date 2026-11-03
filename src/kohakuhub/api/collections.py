"""Collections: curated lists of repositories, compatible with Hugging Face's ``/api/collections``.

What the wire shape follows (read from Hugging Face's OpenAPI spec, ``huggingface_hub`` and the live API, 2026-11-02):

* a collection is ``{owner}/{title-slug}-{24 hex}``; ``{owner}/{title-slug}`` is the short form (and the share URL); the
  same id behind another title answers ``307`` to the canonical slug
* title 1-60 characters, description at most 150, an item's note at most 500 (plain text, answered as ``{text, html}``)
* ``theme`` is one of six colours, drawn at random at creation
* items are models, datasets, Spaces and other collections (a reference: shown as a card, never expanded, so a loop
  of collections is harmless); papers and buckets are ``501``
* ``PATCH`` of a collection answers ``{"success": true, "data": collection}``; creating a title whose slug the owner
  already has is ``409`` with ``{"error", "slug"}``; a list truncates ``items`` to 4, leaves out ``position`` and
  ``shareUrl`` (``huggingface_hub`` passes ``position=`` itself) and pages with an absolute ``Link: <...>; rel="next"``
* errors are ``{"error": message}``

CheeseCave's own additions: ``sort=position`` (the owner's order, for the profile), ``POST/DELETE .../upvote``.
"""

import base64
import json
import operator
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import reduce
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from peewee import JOIN, IntegrityError, Tuple, fn

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
UNSUPPORTED_TYPES = ("paper", "bucket")      # valid on Hugging Face; CheeseCave has no paper catalogue and no buckets
SORTS = ("trending", "upvotes", "lastModified", "position")
TITLE_MAX, DESCRIPTION_MAX, NOTE_MAX = 60, 150, 500
LIST_PREVIEW_ITEMS = 4
TRENDING_DAYS = 7
HEX24 = r"[0-9a-fA-F]{24}"
SLUG_WITH_ID = re.compile(rf"^(.*)-({HEX24})$")
ITEM_FILTER = re.compile(r"^(spaces|models|datasets|papers)/.+")


# ---------------------------------------------------------------- small helpers

def _err(status: int, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": message, **extra},
        headers={"X-Error-Message": message[:300], **(headers or {})},
    )


def _not_found() -> JSONResponse:
    return _err(404, "Collection not found")


async def _json(request: Request, kind: type = dict):
    """The JSON body if it is of type ``kind``, else None."""
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, kind) else None


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _base_url() -> str:
    return cfg.app.base_url.rstrip("/")


def full_slug(col: Collection) -> str:
    return f"{col.owner.username}/{col.slug}-{col.oid}"


def share_url(col: Collection) -> str:
    return f"{_base_url()}/collections/{col.owner.username}/{col.slug}"


def serialize_owner(owner: User) -> dict:
    out = {
        "_id": f"{owner.id:024x}",
        "avatarUrl": f"/api/{'organizations' if owner.is_org else 'users'}/{owner.username}/avatar",
        "fullname": owner.full_name or owner.username,
        "name": owner.username,
        "isHf": False,
        "isHfAdmin": False,
        "isMod": False,
        "type": "org" if owner.is_org else "user",
    }
    if not owner.is_org:
        out["isPro"] = False   # required for a user by the spec, and not allowed for an organization
    return out


def render_note(text: str) -> dict:
    """Plain text with the web addresses turned into links, as the Hub does ({text, html})."""
    html = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" rel="noopener nofollow">\1</a>', escape(text))
    return {"text": text, "html": html.replace("\n", "<br>")}


def _is_position(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ---------------------------------------------------------------- who may see and change what

def _member_of(user: User | None, owner: User, roles: tuple[str, ...]) -> bool:
    if user is None:
        return False
    if owner.id == user.id:
        return True
    return UserOrganization.select().where(
        (UserOrganization.user == user) & (UserOrganization.organization == owner) & (UserOrganization.role.in_(roles))
    ).exists()


READ_ROLES = ("visitor", "member", "admin", "super-admin")     # a visitor reads private things of an organization
WRITE_ROLES = ("member", "admin", "super-admin")                # and cannot write, as for repositories


def can_read(col: Collection, user: User | None) -> bool:
    return (not col.private) or _member_of(user, col.owner, READ_ROLES)


def can_write(col: Collection, user: User | None) -> bool:
    return _member_of(user, col.owner, WRITE_ROLES)


def _visible_collections(user: User | None):
    """SQL predicate: public ones, plus private ones of the caller and of the organizations they belong to."""
    if user is None:
        return Collection.private == False  # noqa: E712
    mine = Collection.owner.in_(
        UserOrganization.select(UserOrganization.organization).where(UserOrganization.user == user)
    ) | (Collection.owner == user)
    return (Collection.private == False) | mine  # noqa: E712


def _with_owner():
    return Collection.select(Collection, User).join(User, on=(Collection.owner == User.id))


# ---------------------------------------------------------------- finding a collection

def resolve(namespace: str, slug: str) -> tuple[Collection | None, bool]:
    """-> (collection, canonical): ``canonical`` is False when the path was the short form or another title."""
    match = SLUG_WITH_ID.match(slug)
    if match:
        col = _with_owner().where((Collection.oid == match.group(2).lower()) & (User.username == namespace)).first()
        return col, bool(col) and col.slug == match.group(1)
    return _with_owner().where((Collection.slug == slug) & (User.username == namespace)).first(), False


def _load(namespace: str, slug: str, user: User | None, request: Request, write: bool = False):
    """-> (collection, None) or (None, response). An unreadable collection is simply not found (as for a repository)."""
    col, canonical = resolve(namespace, slug)
    if col is None or not can_read(col, user):
        return None, _not_found()
    if SLUG_WITH_ID.match(slug) and not canonical:
        # another title in front of the right id: the same request, with the canonical slug (a 307 keeps the method)
        location = request.url.path.replace(f"/collections/{namespace}/{slug}", f"/collections/{full_slug(col)}", 1)
        return None, RedirectResponse(url=location + (f"?{request.url.query}" if request.url.query else ""), status_code=307)
    if write and not can_write(col, user):
        return None, _err(403, "You don't have permission to edit this collection")
    return col, None


# ---------------------------------------------------------------- serialization

def _visible_items(col: Collection, user: User | None) -> list:
    """The items this caller may see: a repository they can read, a collection they can read."""
    rows = list(
        CollectionItem.select(CollectionItem, Repository)
        .join(Repository, JOIN.LEFT_OUTER, on=(CollectionItem.repository == Repository.id))
        .where(CollectionItem.collection == col)
        .order_by(CollectionItem.position, CollectionItem.id)
    )
    repo_ids = [r.repository_id for r in rows if r.repository_id]
    readable = {
        rid
        for (rid,) in Repository.select(Repository.id).where(Repository.id.in_(repo_ids) & repository_read_predicate(user)).tuples()
    } if repo_ids else set()
    child_ids = [r.child_id for r in rows if r.child_id]
    children = {
        c.id: c for c in _with_owner().where(Collection.id.in_(child_ids))
    } if child_ids else {}
    visible = []
    for row in rows:
        if row.repository_id:
            if row.repository_id in readable:
                visible.append(row)
        elif can_read(children[row.child_id], user):
            row.child_loaded = children[row.child_id]
            visible.append(row)
    return visible


def _upvoted(col: Collection, user: User | None) -> bool:
    return bool(user) and CollectionUpvote.select().where(
        (CollectionUpvote.collection == col) & (CollectionUpvote.user == user)
    ).exists()


def _card_text(meta: dict, key: str, default: str, first_of_a_list: bool = False) -> str:
    """A text of a repository card, which anyone writes: a non-empty text, else the default. A ``pipeline_tag`` written as a
    list of one text is seen on a real model, so for that one the first text of a list counts."""
    value = meta.get(key)
    if first_of_a_list and isinstance(value, list) and value:
        value = value[0]
    return value if isinstance(value, str) and value else default


def _serialize_item(row: CollectionItem, info: dict, user: User | None, liked: set) -> dict:
    out = {"_id": row.oid, "position": row.position, "type": row.item_type}
    if row.note:
        out["note"] = render_note(row.note)
    if row.repository_id:
        repo = row.repository
        data = info[repo.id]
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}   # a card can hold anything
        out.update({
            "author": repo.namespace, "id": repo.full_id, "repoType": repo.repo_type, "private": repo.private,
            "likes": repo.likes_count, "lastModified": _iso(datetime.fromisoformat(data["lastModified"])),
            "isLikedByUser": repo.id in liked,
        })
        # The item variants of the spec are closed, and they differ: a model and a dataset have downloads and gated (a
        # model also availableInferenceProviders and maybe a pipeline_tag); a Space has no downloads or gated but needs
        # its colours, emoji, title, createdAt, pinned, featured, disabled, visibility, tags and a runtime. Nothing has
        # `tags` except a Space.
        if repo.repo_type == "space":
            out.update({
                "colorFrom": _card_text(meta, "colorFrom", "gray"), "colorTo": _card_text(meta, "colorTo", "gray"),
                "emoji": _card_text(meta, "emoji", "🧀"), "title": _card_text(meta, "title", repo.name),
                "createdAt": _iso(repo.created_at),
                "pinned": False, "featured": False, "disabled": False, "visibility": "private" if repo.private else "public",
                "tags": [tag for tag in meta["tags"] if isinstance(tag, str)] if isinstance(meta.get("tags"), list) else [],
                "runtime": {"stage": "STOPPED", "hardware": {"current": None, "requested": None}, "replicas": {"current": None, "requested": 1}},   # CheeseCave does not run Spaces
            })
            if meta.get("sdk") in ("gradio", "docker", "static", "streamlit"):
                out["sdk"] = meta["sdk"]
        else:
            out.update({"downloads": repo.downloads, "gated": False})
            if repo.repo_type == "model":
                out["availableInferenceProviders"] = []
                pipeline = _card_text(meta, "pipeline_tag", "", first_of_a_list=True)
                if pipeline:
                    out["pipeline_tag"] = pipeline
    else:
        child = row.child_loaded
        out.update({
            "id": full_slug(child), "slug": full_slug(child), "title": child.title, "owner": serialize_owner(child.owner),
            "lastUpdated": _iso(child.updated_at), "numberItems": len(_visible_items(child, user)), "theme": child.theme,
            "upvotes": child.upvotes_count, "isUpvotedByUser": _upvoted(child, user), "shareUrl": share_url(child),
            **({"description": child.description} if child.description else {}),
        })
    return out


def serialize(col: Collection, user: User | None, limit_items: int | None = None) -> dict:
    """``limit_items`` is set for a list entry, which (like Hugging Face's) has no ``position`` and no ``shareUrl``:
    ``huggingface_hub`` passes ``position=`` itself when it builds a Collection from a list."""
    rows = _visible_items(col, user)[:limit_items]
    repo_ids = [r.repository_id for r in rows if r.repository_id]
    info, liked = {}, set()
    if repo_ids:
        repos = list(Repository.select().where(Repository.id.in_(repo_ids)))
        info = {repo.id: data for repo, data in zip(repos, serialize_items(repos))}   # full_id alone is not unique across types
        if user:
            liked = {
                rid for (rid,) in RepositoryLike.select(RepositoryLike.repository)
                .where((RepositoryLike.user == user) & RepositoryLike.repository.in_(repo_ids)).tuples()
            }
    out = {
        "slug": full_slug(col), "title": col.title, "owner": serialize_owner(col.owner),
        "items": [_serialize_item(r, info, user, liked) for r in rows],
        "lastUpdated": _iso(col.updated_at), "private": col.private, "theme": col.theme,
        "upvotes": col.upvotes_count, "isUpvotedByUser": _upvoted(col, user), "gating": False,
        **({"description": col.description} if col.description else {}),
    }
    if limit_items is None:
        out.update(position=col.position, shareUrl=share_url(col))
    return out


def _reload(col: Collection) -> Collection:
    return _with_owner().where(Collection.id == col.id).get()


def _touch(col: Collection) -> None:
    Collection.update(updated_at=_now()).where(Collection.id == col.id).execute()


def _place(model, scope, subject, position: int, order) -> None:
    """Put ``subject`` at ``position`` among the rows of ``scope`` and renumber the others around it, in order."""
    rows = [r for r in model.select().where(scope).order_by(*order) if r.id != subject.id]
    rows.insert(min(position, len(rows)), subject)
    for index, row in enumerate(rows):
        if row.position != index:
            model.update(position=index).where(model.id == row.id).execute()


# ---------------------------------------------------------------- create, read, update, delete

def _check_text(title=None, description=None) -> JSONResponse | None:
    if title is not None and not (isinstance(title, str) and 1 <= len(title) <= TITLE_MAX):
        return _err(400, f"title must be 1 to {TITLE_MAX} characters")
    if description is not None and not (isinstance(description, str) and len(description) <= DESCRIPTION_MAX):
        return _err(400, f"description must be at most {DESCRIPTION_MAX} characters")
    return None


@router.post("/collections")
async def create_collection(request: Request, user: User = Depends(get_current_user)):
    body = await _json(request)
    if body is None or not isinstance(body.get("title"), str):
        return _err(400, "title is required")
    if (bad := _check_text(body["title"], body.get("description"))) is not None:
        return bad
    if body.get("resourceGroupId") is not None:
        return _no_resource_groups()
    owner = User.get_or_none(User.username == (body.get("namespace") or user.username))
    if owner is None or not _member_of(user, owner, WRITE_ROLES):
        return _err(403, f"You can't create collections for '{body.get('namespace')}'")
    slug = title_slug(body["title"])
    existing = Collection.get_or_none((Collection.owner == owner) & (Collection.slug == slug))
    if existing:
        return _err(409, "The collection already exists", slug=full_slug(existing))
    limit = cfg.app.collection_max_per_owner
    if limit and Collection.select().where(Collection.owner == owner).count() >= limit:
        return _err(400, f"An owner can have at most {limit} collections")
    try:
        with db.atomic():
            col = Collection.create(
                oid=new_oid(), owner=owner, title=body["title"], slug=slug, description=body.get("description") or None,
                theme=secrets.choice(THEMES), private=bool(body.get("private", False)),
                position=Collection.select(fn.COALESCE(fn.MAX(Collection.position) + 1, 0)).where(Collection.owner == owner).scalar(),
            )
            if body.get("item"):
                failure = _add_item(col, body["item"], None, user)
                if failure is not None:
                    raise _Rollback(failure)
    except _Rollback as stop:
        return stop.response
    except IntegrityError:   # the same title slug created by someone else a moment ago
        return _err(409, "The collection already exists", slug=full_slug(Collection.get((Collection.owner == owner) & (Collection.slug == slug))))
    logger.info(f"{user.username} created the collection {col.oid} of {owner.username}")
    return serialize(_reload(col), user)


def _no_resource_groups():
    """Resource groups are an Enterprise feature of Hugging Face; refuse them instead of pretending to apply one."""
    return _err(501, "resource groups are not supported by CheeseCave", {"X-Error-Code": "NotImplemented"})


@router.post("/collections/{namespace}/{slug}/resource-group")
async def set_resource_group(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    _col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    body = await _json(request) or {}
    if body.get("resourceGroupId") is not None:
        return _no_resource_groups()
    return Response(status_code=200)      # no resource group is what every collection has


class _Rollback(Exception):
    """Carries the response of a failure out of a transaction, which this way is rolled back."""

    def __init__(self, response):
        self.response = response


@router.get("/collections/{namespace}/{slug}")
async def get_collection(namespace: str, slug: str, request: Request, user: User | None = Depends(get_optional_user)):
    col, failure = _load(namespace, slug, user, request)
    if failure is not None:
        return failure
    return JSONResponse(serialize(col, user), headers={"ETag": f'W/"{col.oid}-{int(col.updated_at.timestamp())}"'})


@router.patch("/collections/{namespace}/{slug}")
async def update_collection(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    body = await _json(request) or {}
    if (bad := _check_text(body.get("title"), body.get("description"))) is not None:
        return bad
    if "theme" in body and body["theme"] not in THEMES:
        return _err(400, f"theme must be one of {', '.join(THEMES)}")
    if "private" in body and not isinstance(body["private"], bool):
        return _err(400, "private must be true or false")
    if "position" in body and not _is_position(body["position"]):
        return _err(400, "position must be an integer of at least 0")
    changes = {}
    if "title" in body:
        new_slug = title_slug(body["title"])
        clash = Collection.get_or_none((Collection.owner == col.owner) & (Collection.slug == new_slug) & (Collection.id != col.id))
        if clash:
            return _err(409, "The collection already exists", slug=full_slug(clash))
        changes.update(title=body["title"], slug=new_slug)
    if "description" in body:
        changes["description"] = body["description"] or None
    for key in ("theme", "private"):
        if key in body:
            changes[key] = body[key]
    with db.atomic():
        Collection.update(**changes, updated_at=_now()).where(Collection.id == col.id).execute()
        if "position" in body:
            _place(Collection, Collection.owner == col.owner, col, body["position"], (Collection.position, Collection.id))
    return {"success": True, "data": serialize(_reload(col), user)}


@router.delete("/collections/{namespace}/{slug}")
async def delete_collection(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    col.delete_instance()   # its items, its upvotes and the references to it leave by ON DELETE CASCADE
    logger.info(f"{user.username} deleted the collection {col.oid}")
    return Response(status_code=204)


# ---------------------------------------------------------------- items

def _find_target(item, user: User | None):
    """-> (type, repository, child, None) or (None, None, None, failure response)."""
    kind, ident = (item or {}).get("type"), (item or {}).get("id")
    if kind in UNSUPPORTED_TYPES:
        return None, None, None, _err(501, f"{kind} items are not supported by CheeseCave", {"X-Error-Code": "NotImplemented"})
    if kind not in REPO_TYPES + ("collection",) or not isinstance(ident, str) or not ident:
        return None, None, None, _err(400, "item must be {type: model|dataset|space|collection, id: string}")
    namespace, _, rest = ident.partition("/")
    if kind == "collection":
        child, _canonical = resolve(namespace, rest)
        if child is None or not can_read(child, user):
            return None, None, None, _err(404, "Collection not found")
        return kind, None, child, None
    repo = Repository.get_or_none((Repository.repo_type == kind) & (Repository.namespace == namespace) & (Repository.name == rest))
    if repo is None or not Repository.select().where((Repository.id == repo.id) & repository_read_predicate(user)).exists():
        return None, None, None, _err(404, "Repository not found")   # one answer for "absent" and "private": no leak
    return kind, repo, None, None


def _add_item(col: Collection, item, note, user: User):
    if note is not None and not (isinstance(note, str) and len(note) <= NOTE_MAX):
        return _err(400, f"note must be at most {NOTE_MAX} characters")
    kind, repo, child, failure = _find_target(item, user)
    if failure is not None:
        return failure
    if child is not None and child.id == col.id:
        return _err(400, "A collection can't contain itself")
    limit = cfg.app.collection_max_items
    if limit and CollectionItem.select().where(CollectionItem.collection == col).count() >= limit:
        return _err(400, f"A collection can have at most {limit} items")
    same = (CollectionItem.repository == repo) if repo is not None else (CollectionItem.child == child)
    if CollectionItem.select().where((CollectionItem.collection == col) & same).exists():
        return _err(409, "Item already exists in the collection")
    top = CollectionItem.select(fn.MAX(CollectionItem.position)).where(CollectionItem.collection == col).scalar()
    CollectionItem.create(
        oid=new_oid(), collection=col, item_type=kind, repository=repo, child=child, note=note or None,
        position=0 if top is None else top + 1,
    )
    _touch(col)
    return None


@router.post("/collections/{namespace}/{slug}/items")
async def add_item(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    body = await _json(request) or {}
    try:
        with db.atomic():
            failure = _add_item(col, body.get("item"), body.get("note"), user)
    except IntegrityError:   # the same item added by another request a moment ago
        return _err(409, "Item already exists in the collection")
    return failure or serialize(_reload(col), user)


def _update_item(col: Collection, item: CollectionItem, data):
    data = data if isinstance(data, dict) else {}
    note = data.get("note")
    if note is not None and not (isinstance(note, str) and len(note) <= NOTE_MAX):
        return _err(400, f"note must be at most {NOTE_MAX} characters")
    if "position" in data and not _is_position(data["position"]):
        return _err(400, "position must be an integer of at least 0")
    if note is not None:
        CollectionItem.update(note=note or None).where(CollectionItem.id == item.id).execute()
    if "position" in data:
        _place(CollectionItem, CollectionItem.collection == col, item, data["position"], (CollectionItem.position, CollectionItem.id))
    _touch(col)   # `gallery` (image addresses of an item) is accepted and not stored yet
    return None


def _find_item(col: Collection, item_id: str) -> CollectionItem | None:
    return CollectionItem.get_or_none((CollectionItem.collection == col) & (CollectionItem.oid == str(item_id).lower()))


@router.patch("/collections/{namespace}/{slug}/items/{item_id}")
async def update_item(namespace: str, slug: str, item_id: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    item = _find_item(col, item_id)
    if item is None:
        return _err(404, "Item not found")
    with db.atomic():
        failure = _update_item(col, item, await _json(request))
    return failure or JSONResponse(content={})


@router.post("/collections/{namespace}/{slug}/items/batch")
async def batch_items(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request, write=True)
    if failure is not None:
        return failure
    actions = await _json(request, list)
    if actions is None:
        return _err(400, "body must be a list of {action, _id, data}")
    try:
        with db.atomic():   # all or nothing
            for action in actions:
                if not isinstance(action, dict) or action.get("action") != "update":
                    raise _Rollback(_err(400, "only the action 'update' exists"))
                item = _find_item(col, action.get("_id", ""))
                if item is None:
                    raise _Rollback(_err(404, "Item not found"))
                failure = _update_item(col, item, action.get("data"))
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
    item = _find_item(col, item_id)
    if item is None:
        return _err(404, "Item not found")
    item.delete_instance()   # the other positions stay as they are: gaps are normal
    _touch(col)
    return Response(status_code=204)


# ---------------------------------------------------------------- upvotes (the web page; no Hugging Face client method)

def _change_upvote(col: Collection, user: User, give: bool) -> dict:
    with db.atomic():
        if give and not _upvoted(col, user):
            try:
                with db.atomic():   # a savepoint: on PostgreSQL a failed insert would abort the whole transaction
                    CollectionUpvote.create(collection=col, user=user)
            except IntegrityError:   # the same user upvoted a moment ago (a double click): it is one upvote
                pass
        elif not give:
            CollectionUpvote.delete().where((CollectionUpvote.collection == col) & (CollectionUpvote.user == user)).execute()
        count = CollectionUpvote.select().where(CollectionUpvote.collection == col).count()
        Collection.update(upvotes_count=count).where(Collection.id == col.id).execute()   # counted, not incremented: never drifts
    return {"success": True, "upvotes": count}


@router.post("/collections/{namespace}/{slug}/upvote")
async def upvote(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request)
    return failure or _change_upvote(col, user, True)


@router.delete("/collections/{namespace}/{slug}/upvote")
async def remove_upvote(namespace: str, slug: str, request: Request, user: User = Depends(get_current_user)):
    col, failure = _load(namespace, slug, user, request)
    return failure or _change_upvote(col, user, False)


# ---------------------------------------------------------------- listing

def _encode_cursor(sort: str, key: list) -> str:
    return base64.urlsafe_b64encode(json.dumps({"s": sort, "k": key}).encode()).decode().rstrip("=")


def _decode_cursor(text: str, sort: str, kinds: tuple) -> list | None:
    """The key a cursor of this ``sort`` carries (one value of each of ``kinds``), or None when it is not one."""
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        key = list(data["k"])
        if data["s"] != sort or len(key) != len(kinds):
            return None
        return [kind(value) if kind is not datetime else datetime.fromisoformat(value) for kind, value in zip(kinds, key)]
    except (ValueError, KeyError, TypeError):
        return None


@router.get("/collections")
async def list_collections(request: Request, user: User | None = Depends(get_optional_user)):
    q = request.query_params
    owners, items = q.getlist("owner"), q.getlist("item")
    sort, expand = q.get("sort", "trending"), q.get("expand", "true").lower() != "false"
    if sort not in SORTS:   # "position" is CheeseCave's: the owner's own order (Hugging Face's list answers 400 to it)
        return _err(400, 'sort must be one of "upvotes", "lastModified", "trending"')
    try:
        limit = int(q.get("limit", 10))
    except ValueError:
        return _err(400, "limit must be a number")
    if not 1 <= limit <= 10000:
        return _err(400, "limit must be between 1 and 10000")
    if limit > 100 and not (not expand and owners):
        return _err(400, "Limit must be less than or equal to 100 unless expand is false and owner is set")
    if any(not ITEM_FILTER.match(ref) for ref in items):
        return _err(400, "item must look like models/<id>, datasets/<id>, spaces/<id> or papers/<id>")

    query = _with_owner().where(_visible_collections(user))
    if owners:
        query = query.where(User.username.in_(owners))
    if q.get("q"):
        term = f"%{q['q']}%"   # ilike: case-insensitive on SQLite (a plain like is a case-sensitive GLOB there) and PostgreSQL
        query = query.where(Collection.title.ilike(term) | Collection.description.ilike(term))
    if items:   # several items: a collection holding any of them (as on Hugging Face), and a paper is held by none
        matches = []
        for ref in items:
            kind, _, ident = ref.partition("/")
            if kind == "papers":
                continue
            namespace, _, name = ident.partition("/")
            hit = (
                CollectionItem.select(CollectionItem.collection)
                .join(Repository, on=(CollectionItem.repository == Repository.id))
                .where(
                    (Repository.repo_type == kind[:-1]) & (Repository.namespace == namespace) & (Repository.name == name)
                    & repository_read_predicate(user)   # a repository the caller cannot read matches nothing, as if it did not exist
                )
            )
            matches.append(Collection.id.in_(hit))
        query = query.where(reduce(operator.or_, matches) if matches else Collection.id == -1)

    # The order is a key; a cursor holds the key of the last row, so a page never repeats or skips a row when
    # collections change between two requests. (descending keys compare with <, the owner's order with >)
    if sort == "position":
        columns, kinds, descending = (User.username, Collection.position, Collection.id), (str, int, int), False
    elif sort == "lastModified":
        columns, kinds, descending = (Collection.updated_at, Collection.id), (datetime, int), True
    elif sort == "upvotes":
        columns, kinds, descending = (Collection.upvotes_count, Collection.updated_at, Collection.id), (int, datetime, int), True
    else:   # trending: upvotes of the last week, then the latest change
        recent = (
            CollectionUpvote.select(CollectionUpvote.collection, fn.COUNT(CollectionUpvote.id).alias("recent"))
            .where(CollectionUpvote.created_at >= _now() - timedelta(days=TRENDING_DAYS))
            .group_by(CollectionUpvote.collection).alias("r")
        )
        query = query.join(recent, JOIN.LEFT_OUTER, on=(Collection.id == recent.c.collection_id))
        columns, kinds, descending = (fn.COALESCE(recent.c.recent, 0), Collection.updated_at, Collection.id), (int, datetime, int), True
    if q.get("cursor"):
        after = _decode_cursor(q["cursor"], sort, kinds)
        if after is None:
            return _err(400, "invalid cursor")
        query = query.where(Tuple(*columns) < tuple(after) if descending else Tuple(*columns) > tuple(after))
    query = query.order_by(*(c.desc() if descending else c.asc() for c in columns))

    rows = list(query.limit(limit + 1))
    more, rows = len(rows) > limit, rows[:limit]
    if not expand and owners:
        body = [{"name": full_slug(c), "private": c.private, "title": c.title, "upvotes": c.upvotes_count} for c in rows]
    else:
        body = [serialize(c, user, limit_items=LIST_PREVIEW_ITEMS) for c in rows]
    headers = {}
    if more:
        last = rows[-1]
        key = {
            "position": [last.owner.username, last.position, last.id],
            "lastModified": [last.updated_at.isoformat(), last.id],
            "upvotes": [last.upvotes_count, last.updated_at.isoformat(), last.id],
        }.get(sort)
        if key is None:   # trending: the score is not a column of the row; read it back with the same window
            key = [_trending_score(last), last.updated_at.isoformat(), last.id]
        params = [(k, v) for k, v in q.multi_items() if k != "cursor"] + [("cursor", _encode_cursor(sort, key))]
        headers["Link"] = f'<{_base_url()}{request.url.path}?{urlencode(params)}>; rel="next"'
    return JSONResponse(body, headers=headers)


def _trending_score(col: Collection) -> int:
    return CollectionUpvote.select().where(
        (CollectionUpvote.collection == col) & (CollectionUpvote.created_at >= _now() - timedelta(days=TRENDING_DAYS))
    ).count()
