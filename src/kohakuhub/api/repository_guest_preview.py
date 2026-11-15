"""Fixed, controlled previews for repository browser guests.

Only the current main snapshot is browsable. Tree and main-history metadata are
paginated with scoped signed cursors. The root README card has a fixed 64 KiB /
1000-line ceiling; other text is plain UTF-8 (8 KiB / 64 lines),
CSV/TSV/JSONL expose at most 20 rows and columns with 256 characters per cell, and
small PNG/JPEG/WebP/GIF files become a metadata-free, single-frame PNG thumbnail.
PDF pages and audio/video are rendered through private derivative routes.
Other LFS-backed content, SVG, Parquet, archives, model weights and browser
model inference remain metadata-only. Nothing here
returns original file addresses, presigned URLs, checksums, arbitrary ranges or
caller-controlled offsets. Displayed content can still be copied or saved.
"""

import asyncio
import base64
import codecs
import csv
import hashlib
import hmac
import io
import json
import math
import re
import unicodedata
from datetime import timezone
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image, UnidentifiedImageError

from kohakuhub import repository_media
from kohakuhub.api.repository_access import BASE, RepoType, guest_for_request
from kohakuhub.config import cfg
from kohakuhub.db import Commit, File, PathCommit, RepositoryCard, RepositoryCreator
from kohakuhub.db_operations import get_repository
from kohakuhub.lfs_gc import lfs_oid
from kohakuhub.utils.lakefs import get_lakefs_client, lakefs_status, resolve_lakefs_repo

router = APIRouter()
STORAGE_TIMEOUT_SECONDS = 5.0
MAX_TREE_ENTRIES = 100
MAX_TEXT_BYTES = 8192
MAX_TEXT_LINES = 64
MAX_DATA_BYTES = 65536
MAX_DATA_ROWS = 20
MAX_DATA_COLUMNS = 20
MAX_CELL_CHARS = 256
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_THUMBNAIL_SIDE = 512
MAX_CARD_BYTES = 65536
MAX_CARD_LINES = 1000
MAX_COMMIT_ENTRIES = 20
CARD_METADATA_KEYS = {
    "license", "license_name", "language", "tags", "pipeline_tag", "library_name",
    "base_model", "datasets", "metrics", "model-index", "dataset_info", "configs",
    "task_categories", "size_categories", "pretty_name", "annotations_creators",
    "language_creators", "multilinguality", "sdk", "sdk_version", "app_file",
}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".html", ".htm", ".css", ".scss", ".sass",
    ".less", ".sh", ".bash", ".zsh", ".ps1", ".sql", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".java", ".rs", ".go", ".rb", ".r", ".xml", ".log", ".ipynb",
}
TEXT_NAMES = {"readme", "license", "licence", "notice", "authors", "dockerfile", "makefile", ".gitignore", ".gitattributes"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ROW_SUFFIXES = {".csv", ".tsv", ".jsonl", ".ndjson"}
UNSUPPORTED_MESSAGE = "This file is available as metadata only in guest preview. Original files and downloads require an authorized account."


def _unavailable():
    return HTTPException(404, detail={"error": "Repository preview not found"}, headers={"X-Error-Code": "RepoNotFound"})


def _authorize(repo_type, namespace, name, request):
    repo = get_repository(repo_type, namespace, name)
    if repo is None or guest_for_request(repo, request) is None:
        raise _unavailable()
    return repo


def _path(value, *, root=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise HTTPException(400, detail={"error": "Invalid repository-relative preview path"})
    if root and value == "":
        return value
    if (
        not value or value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value)
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise HTTPException(400, detail={"error": "Invalid repository-relative preview path"})
    return value


def _parameters(request, *, root=False):
    pairs = list(request.query_params.multi_items())
    if "range" in request.headers or any(key != "path" for key, _ in pairs) or len(pairs) > 1:
        raise HTTPException(400, detail={"error": "Guest previews do not accept ranges, revisions, offsets or pagination"})
    return _path(request.query_params.get("path", ""), root=root)


def _browse_parameters(request, allowed):
    pairs = list(request.query_params.multi_items())
    if "range" in request.headers or any(key not in allowed for key, _ in pairs) or len({key for key, _ in pairs}) != len(pairs):
        raise HTTPException(400, detail={"error": "Invalid guest browsing parameters"})
    return dict(pairs)


def _cursor_token(context, after):
    raw = json.dumps({**context, "after": after}, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signature = hmac.new(cfg.auth.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def _cursor_after(token, context):
    if not token:
        return ""
    try:
        if len(token) > 8192:
            raise ValueError
        body, signature = token.split(".")
        expected = hmac.new(cfg.auth.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != set(context) | {"after"}:
            raise ValueError
        if any(payload[key] != value for key, value in context.items() if key != "head"):
            raise ValueError
        if payload["head"] != context["head"]:
            raise HTTPException(409, detail={"error": "Repository changed; restart browsing"})
        after = payload["after"]
        if not isinstance(after, str) or not after or len(after) > 2048:
            raise ValueError
        return after
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, detail={"error": "Invalid guest browsing cursor"}) from None


def _iso(value):
    # The shared DB field returns naive UTC, never execution-host local time.
    return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None


def _safe_card_value(value, depth=0):
    if depth > 6:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, str):
        return None if "://" in value or len(value) > 4096 else value
    if isinstance(value, list):
        return [_safe_card_value(item, depth + 1) for item in value[:100]]
    # Only JSON values enter this helper; objects are the remaining JSON type.
    return {key: _safe_card_value(item, depth + 1) for key, item in list(value.items())[:100]
            if len(key) <= 128 and key.lower() not in {"physical_address", "checksum", "url", "token", "download_url", "presigned_url"}}


def _card_metadata(repo, head):
    row = RepositoryCard.get_or_none(RepositoryCard.repository == repo.id)
    if row is None or row.main_sha != head or len(row.card) > MAX_CARD_BYTES:
        return {}
    try:
        metadata = json.loads(row.card)
    except (ValueError, TypeError, RecursionError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    return {key: _safe_card_value(value) for key, value in metadata.items() if key in CARD_METADATA_KEYS}


async def _readme(repo, client, source, head):
    if head is None:
        return None
    for path in ("README.md", "readme.md", "Readme.md"):
        record = _file(repo, path)
        if record is not None and (record.is_deleted or record.lfs):
            continue
        try:
            metadata = await _storage(client.stat_object(repository=source, ref=head, path=path, user_metadata=False))
        except HTTPException as error:
            if error.status_code == 404:
                continue
            raise
        if not isinstance(metadata, dict):
            raise HTTPException(503, detail={"error": "Guest card content is unavailable"})
        if metadata.get("path", path) != path or metadata.get("path_type", "object") != "object" or _is_lfs(metadata, record):
            continue
        raw = await _storage(client.get_object_prefix(source, head, path, MAX_CARD_BYTES + 1))
        if not isinstance(raw, bytes):
            raise HTTPException(503, detail={"error": "Guest card content is unavailable"})
        raw = raw[:MAX_CARD_BYTES + 1]
        if raw.removeprefix(b"\xef\xbb\xbf").startswith(b"version https://git-lfs.github.com/spec/"):
            return None
        size = _size(metadata)
        complete = len(raw) <= MAX_CARD_BYTES and (size is None or size <= len(raw))
        try:
            text = _utf8(raw[:MAX_CARD_BYTES], complete=complete)
        except (UnicodeDecodeError, ValueError):
            return None
        lines = text.splitlines(keepends=True)
        return {"path": path, "text": "".join(lines[:MAX_CARD_LINES]), "truncated": not complete or len(lines) > MAX_CARD_LINES}
    return None


@router.get(BASE + "/guest/overview")
async def guest_overview(repo_type: RepoType, namespace: str, name: str, request: Request, response: Response):
    repo = _authorize(repo_type, namespace, name, request)
    _browse_parameters(request, set())
    _headers(response)
    client, source, head = await _snapshot(repo)
    readme = await _readme(repo, client, source, head)
    commit = Commit.get_or_none((Commit.repository == repo.id) & (Commit.commit_id == head)) if head else None
    creator = RepositoryCreator.get_or_none(RepositoryCreator.repository == repo.id)
    payload = {
        "id": repo.full_id, "type": repo.repo_type, "author": repo.namespace,
        "private": bool(repo.private), "invite_mode": bool(repo.invite_mode),
        "created_at": _iso(repo.created_at), "last_modified": _iso(commit.created_at) if commit else None,
        "created_by": creator.user.username if creator and creator.user_id else None,
        "downloads": max(0, repo.downloads), "likes": max(0, repo.likes_count),
        "used_storage": max(0, repo.used_bytes), "default_branch": "main", "head": head,
        "file_count": File.select().where((File.repository == repo.id) & (File.branch == "main") & (File.is_deleted == False)).count(),
        "readme": readme, "metadata": _card_metadata(repo, head),
    }
    _authorize(repo_type, namespace, name, request)
    return payload


def _headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie"
    response.headers["X-Content-Type-Options"] = "nosniff"


async def _storage(operation):
    try:
        return await asyncio.wait_for(operation, timeout=STORAGE_TIMEOUT_SECONDS)
    except Exception as error:
        if lakefs_status(error) == 404:
            raise HTTPException(404, detail={"error": "Preview entry not found"}) from None
        raise HTTPException(503, detail={"error": "Guest preview storage is temporarily unavailable"}) from None


async def _snapshot(repo):
    client = get_lakefs_client()
    source = resolve_lakefs_repo(repo)
    branch = await _storage(client.get_branch(repository=source, branch="main"))
    if not isinstance(branch, dict):
        raise HTTPException(503, detail={"error": "Guest preview snapshot is unavailable"})
    head = branch.get("commit_id")
    if head is not None and (not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40,64}", head)):
        raise HTTPException(503, detail={"error": "Guest preview snapshot is unavailable"})
    return client, source, head


def _size(metadata):
    value = metadata.get("size_bytes")
    return value if type(value) is int and value >= 0 else None


def _kind(path, size, lfs=False):
    media = repository_media.media_kind(path)
    if media:
        return media
    if lfs:
        return "unsupported"
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image" if size is not None and size <= MAX_IMAGE_BYTES else "unsupported"
    if suffix in ROW_SUFFIXES:
        return "rows"
    if suffix in TEXT_SUFFIXES or PurePosixPath(path).name.lower() in TEXT_NAMES:
        return "text"
    return "unsupported"


def _unsupported(size):
    return {"kind": "unsupported", "message": UNSUPPORTED_MESSAGE, "size": size}


def _file(repo, path):
    return File.get_or_none((File.repository == repo.id) & (File.branch == "main") & (File.path_in_repo == path))


def _is_lfs(metadata, record):
    address = metadata.get("physical_address")
    if address is not None and not isinstance(address, str):
        raise HTTPException(503, detail={"error": "Preview storage metadata is unavailable"})
    return bool((record is not None and record.lfs) or lfs_oid(address))


def _utf8(raw, *, complete):
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    text = decoder.decode(raw, final=complete)
    if any(unicodedata.category(character) == "Cc" and character not in "\t\r\n" for character in text):
        raise ValueError("Binary content")
    return text


def _text(raw, size):
    visible = raw[:MAX_TEXT_BYTES]
    complete = len(raw) <= MAX_TEXT_BYTES and (size is None or size <= len(raw))
    try:
        text = _utf8(visible, complete=complete)
    except (UnicodeDecodeError, ValueError):
        return None
    lines = text.splitlines(keepends=True)
    return {"kind": "text", "text": "".join(lines[:MAX_TEXT_LINES]),
            "truncated": not complete or len(lines) > MAX_TEXT_LINES}


def _cell(value):
    if value is None:
        value = ""
    elif not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value[:MAX_CELL_CHARS], len(value) > MAX_CELL_CHARS


def _rows(raw, size, suffix):
    truncated = len(raw) > MAX_DATA_BYTES or (size is not None and size > len(raw))
    try:
        text = _utf8(raw[:MAX_DATA_BYTES], complete=not truncated)
        if truncated and not text.endswith(("\n", "\r")):
            text = text.rsplit("\n", 1)[0] if "\n" in text else ""
        if suffix in {".csv", ".tsv"}:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t" if suffix == ".tsv" else ",", strict=True)
            header = next(reader, [])
            columns = header[:MAX_DATA_COLUMNS]
            truncated |= len(header) > MAX_DATA_COLUMNS
            rows = []
            for record in reader:
                if len(rows) == MAX_DATA_ROWS:
                    truncated = True
                    break
                truncated |= len(record) > MAX_DATA_COLUMNS
                rows.append(record[:MAX_DATA_COLUMNS])
            rows = [row[:len(columns)] + [""] * max(0, len(columns) - len(row)) for row in rows]
        else:
            records, columns = [], []
            for line in text.splitlines():
                if not line.strip():
                    continue
                if len(records) == MAX_DATA_ROWS:
                    truncated = True
                    break
                record = json.loads(line)
                if not isinstance(record, dict):
                    return None
                for key in record:
                    if key not in columns:
                        if len(columns) < MAX_DATA_COLUMNS:
                            columns.append(key)
                        else:
                            truncated = True
                records.append(record)
            rows = [[record.get(key) for key in columns] for record in records]
        result = []
        labels = []
        for label in columns:
            shown, clipped = _cell(label)
            labels.append(shown)
            truncated |= clipped
        for row in rows:
            cells = []
            for cell in row:
                shown, clipped = _cell(cell)
                cells.append(shown)
                truncated |= clipped
            result.append(cells)
        return {"kind": "rows", "columns": labels, "rows": result, "truncated": truncated}
    except (UnicodeDecodeError, ValueError, csv.Error, RecursionError):
        return None


def _thumbnail(raw):
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"} or image.width * image.height > MAX_IMAGE_PIXELS:
                return None
            image.seek(0)
            frame = image.convert("RGBA")
            frame.thumbnail((MAX_THUMBNAIL_SIDE, MAX_THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
            # Saving a new image, rather than the source or its info dict, strips EXIF,
            # PNG text, profiles, animation and embedded file addresses.
            clean = Image.new("RGBA", frame.size)
            clean.paste(frame)
            output = io.BytesIO()
            clean.save(output, format="PNG")
            return {"kind": "image", "data_url": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
                    "width": clean.width, "height": clean.height}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


@router.get(BASE + "/guest/tree")
async def guest_tree(repo_type: RepoType, namespace: str, name: str, request: Request, response: Response):
    repo = _authorize(repo_type, namespace, name, request)
    params = _browse_parameters(request, {"path", "search", "cursor"})
    path = _path(params.get("path", ""), root=True)
    search = params.get("search", "")
    if len(search) > 128 or "/" in search or "\\" in search or any(unicodedata.category(character) in {"Cc", "Cs"} for character in search):
        raise HTTPException(400, detail={"error": "Invalid guest filename search"})
    _headers(response)
    client, source, head = await _snapshot(repo)
    folder_prefix = path + "/" if path else ""
    prefix = folder_prefix + search
    context = {"kind": "tree", "repository": repo.id, "head": head, "path": path, "search": search}
    after = _cursor_after(params.get("cursor"), context)
    if after:
        after_path = _path(after[:-1] if after.endswith("/") else after)
        if not after_path.startswith(prefix):
            raise HTTPException(400, detail={"error": "Invalid guest browsing cursor"})
    if head is None:
        payload = {"entries": [], "path": path, "has_more": False}
    else:
        listing = await _storage(client.list_objects(repository=source, ref=head, prefix=prefix, delimiter="/", amount=MAX_TREE_ENTRIES,
                                                     **({"after": after} if after else {})))
        objects, storage_more = _listing_page(listing, "Guest preview listing is unavailable")
        candidates, last_position = [], ""
        for obj in objects[:MAX_TREE_ENTRIES]:
            if not isinstance(obj, dict) or obj.get("path_type") not in {"object", "common_prefix"}:
                raise HTTPException(503, detail={"error": "Guest preview listing is unavailable"})
            directory = obj["path_type"] == "common_prefix"
            raw_path = obj.get("path", "")
            raw_path = raw_path[:-1] if directory and isinstance(raw_path, str) and raw_path.endswith("/") else raw_path
            try:
                entry_path = _path(raw_path)
            except HTTPException:
                raise HTTPException(503, detail={"error": "Guest preview listing is unavailable"}) from None
            if not entry_path.startswith(prefix):
                raise HTTPException(503, detail={"error": "Guest preview listing is unavailable"})
            last_position = entry_path + "/" if directory else entry_path
            relative = entry_path[len(folder_prefix):]
            if not relative or "/" in relative:
                continue  # An ignored delimiter never turns this into a recursive listing.
            candidates.append((entry_path, directory, obj))
        records = {record.path_in_repo: record for record in File.select().where(
            (File.repository == repo.id) & (File.branch == "main") & File.path_in_repo.in_([path for path, directory, _ in candidates if not directory])
        )}
        last_commits = _path_commits(repo, [path for path, _, _ in candidates], head)
        entries, seen = [], set()
        for entry_path, directory, obj in candidates:
            record = records.get(entry_path)
            if entry_path in seen or (record is not None and record.is_deleted):
                continue
            seen.add(entry_path)
            size = 0 if directory else _size(obj)
            entries.append({"path": entry_path, "type": "directory" if directory else "file", "size": size,
                            "preview_kind": None if directory else _kind(entry_path, size, _is_lfs(obj, record))})
            if entry_path in last_commits:
                entries[-1]["last_commit"] = last_commits[entry_path]
        more = len(objects) > MAX_TREE_ENTRIES or storage_more
        payload = {"entries": entries, "path": path, "has_more": more}
        if more:
            if not last_position or last_position == after:
                raise HTTPException(503, detail={"error": "Guest preview listing cannot advance"})
            payload["next_cursor"] = _cursor_token(context, last_position)
    _authorize(repo_type, namespace, name, request)
    return payload


def _commit_author(value):
    if not isinstance(value, str):
        return "unknown"
    name = value.split("<", 1)[0].strip()[:200]
    if not name or "@" in name or "://" in name or any(unicodedata.category(character) in {"Cc", "Cs"} for character in name):
        return "unknown"
    return name


def _path_commits(repo, paths, head):
    rows = [row for row in PathCommit.select().where(
        (PathCommit.repository == repo.id) & (PathCommit.branch == "main") & PathCommit.path.in_(paths)
    ) if re.fullmatch(r"[0-9a-f]{40,64}", row.commit_id)
        # A cached path commit can precede a pruned history fence. Only expose
        # commits demonstrably visible without fetching unbounded history.
        and (not repo.history_root or row.commit_id in {head, repo.history_root})]
    authors = {row.commit_id: row.username for row in Commit.select().where(
        (Commit.repository == repo.id) & (Commit.branch == "main") & Commit.commit_id.in_([row.commit_id for row in rows])
    )}
    return {row.path: {"id": row.commit_id, "title": row.title[:16000], "timestamp": row.date,
                       "author": _commit_author(authors.get(row.commit_id))} for row in rows}


@router.get(BASE + "/guest/file-info")
async def guest_file_info(repo_type: RepoType, namespace: str, name: str, request: Request, response: Response):
    repo = _authorize(repo_type, namespace, name, request)
    path = _parameters(request)
    _headers(response)
    record = _file(repo, path)
    if record is not None and record.is_deleted:
        raise HTTPException(404, detail={"error": "Preview entry not found"})
    client, source, head = await _snapshot(repo)
    if head is None:
        raise HTTPException(404, detail={"error": "Preview entry not found"})
    metadata = await _storage(client.stat_object(repository=source, ref=head, path=path, user_metadata=False))
    if not isinstance(metadata, dict):
        raise HTTPException(503, detail={"error": "Guest file metadata is unavailable"})
    if metadata.get("path", path) != path or metadata.get("path_type", "object") != "object":
        raise HTTPException(404, detail={"error": "Preview entry not found"})
    payload = {"path": path, "size": _size(metadata), "last_modified": _iso(record.updated_at) if record else None}
    last_commit = _path_commits(repo, [path], head).get(path)
    if last_commit:
        payload["last_commit"] = last_commit
    _authorize(repo_type, namespace, name, request)
    return payload


def _listing_page(listing, message):
    if not isinstance(listing, dict) or not isinstance(listing.get("results", []), list):
        raise HTTPException(503, detail={"error": message})
    pagination = listing.get("pagination", {})
    if not isinstance(pagination, dict) or type(pagination.get("has_more", False)) is not bool:
        raise HTTPException(503, detail={"error": message})
    return listing.get("results", []), pagination.get("has_more", False)


@router.get(BASE + "/guest/commits")
async def guest_commits(repo_type: RepoType, namespace: str, name: str, request: Request, response: Response):
    repo = _authorize(repo_type, namespace, name, request)
    params = _browse_parameters(request, {"cursor"})
    _headers(response)
    client, source, head = await _snapshot(repo)
    context = {"kind": "commits", "repository": repo.id, "head": head, "history_root": repo.history_root}
    after = _cursor_after(params.get("cursor"), context)
    if after and not re.fullmatch(r"[0-9a-f]{40,64}", after):
        raise HTTPException(400, detail={"error": "Invalid guest browsing cursor"})
    if head is None:
        payload = {"commits": [], "has_more": False, "next_cursor": None}
    else:
        listing = await _storage(client.log_commits(repository=source, ref=head, amount=MAX_COMMIT_ENTRIES,
                                                   **({"after": after} if after else {})))
        objects, storage_more = _listing_page(listing, "Guest commit history is unavailable")
        selected = objects[:MAX_COMMIT_ENTRIES]
        for item in selected:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not re.fullmatch(r"[0-9a-f]{40,64}", item["id"]):
                raise HTTPException(503, detail={"error": "Guest commit history is unavailable"})
        local = {row.commit_id: row for row in Commit.select().where((Commit.repository == repo.id) & Commit.commit_id.in_([item["id"] for item in selected]))}
        commits, boundary = [], False
        for item in selected:
            record = local.get(item["id"])
            parents = item.get("parents", [])
            commits.append({"id": item["id"], "message": item.get("message", "")[:16000] if isinstance(item.get("message", ""), str) else "",
                            "timestamp": item.get("creation_date") if type(item.get("creation_date")) in {int, float} and math.isfinite(item["creation_date"]) else None,
                            "author": _commit_author(record.username if record else item.get("committer")),
                            "parents": [value for value in parents[:16] if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40,64}", value)] if isinstance(parents, list) else []})
            if repo.history_root and item["id"] == repo.history_root:
                commits[-1]["parents"] = []
                boundary = True
                break
        more = not boundary and (len(objects) > MAX_COMMIT_ENTRIES or storage_more)
        if more and (not commits or commits[-1]["id"] == after):
            raise HTTPException(503, detail={"error": "Guest commit history cannot advance"})
        payload = {"commits": commits, "has_more": more,
                   "next_cursor": _cursor_token(context, commits[-1]["id"]) if more else None}
    _authorize(repo_type, namespace, name, request)
    return payload


@router.get(BASE + "/guest/preview")
async def guest_preview(repo_type: RepoType, namespace: str, name: str, request: Request, response: Response):
    repo = _authorize(repo_type, namespace, name, request)
    path = _parameters(request)
    _headers(response)
    record = _file(repo, path)
    if record is not None and record.is_deleted:
        raise HTTPException(404, detail={"error": "Preview entry not found"})
    client, source, head = await _snapshot(repo)
    if head is None:
        raise HTTPException(404, detail={"error": "Preview entry not found"})
    metadata = await _storage(client.stat_object(repository=source, ref=head, path=path, user_metadata=False))
    if metadata.get("path", path) != path or metadata.get("path_type", "object") != "object":
        raise HTTPException(404, detail={"error": "Preview entry not found"})
    size = _size(metadata)
    kind = _kind(path, size, _is_lfs(metadata, record))
    payload = _unsupported(size)
    if kind in {"pdf", "video", "audio"}:
        payload = await repository_media.describe(repo, client, source, head, path, metadata, record)
        _, _, current_head = await _snapshot(repo)
        if current_head != head:
            raise HTTPException(409, detail={"error": "Repository changed; reopen the file"})
    elif kind != "unsupported":
        limit = {"text": MAX_TEXT_BYTES, "rows": MAX_DATA_BYTES, "image": MAX_IMAGE_BYTES}[kind]
        raw = await _storage(client.get_object_prefix(source, head, path, limit + 1))
        if not isinstance(raw, bytes):
            raise HTTPException(503, detail={"error": "Guest preview content is unavailable"})
        raw = raw[:limit + 1]
        if not raw.removeprefix(b"\xef\xbb\xbf").startswith(b"version https://git-lfs.github.com/spec/"):
            match kind:
                case "text":
                    payload = _text(raw, size) or payload
                case "rows":
                    payload = _rows(raw, size, PurePosixPath(path).suffix.lower()) or payload
                case "image" if len(raw) <= MAX_IMAGE_BYTES and len(raw) == size:
                    payload = await _storage(asyncio.to_thread(_thumbnail, raw)) or payload
    _authorize(repo_type, namespace, name, request)
    return payload
