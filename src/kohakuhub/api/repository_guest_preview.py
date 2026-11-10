"""Fixed, controlled previews for repository browser guests.

Only the current main snapshot is browsable. Text is plain UTF-8 (8 KiB / 64 lines),
CSV/TSV/JSONL expose at most 20 rows and columns with 256 characters per cell, and
small PNG/JPEG/WebP/GIF files become a metadata-free, single-frame PNG thumbnail.
LFS-backed content, SVG, PDF, audio/video, Parquet, archives, model weights and
browser model inference are metadata-only in this first release. Nothing here
returns original file addresses, presigned URLs, checksums, arbitrary ranges or
caller-controlled offsets. Displayed content can still be copied or saved.
"""

import asyncio
import base64
import codecs
import csv
import io
import json
import re
import unicodedata
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image, UnidentifiedImageError

from kohakuhub.api.repository_access import BASE, RepoType, guest_for_request
from kohakuhub.db import File
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
    head = branch.get("commit_id")
    if head is not None and (not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40,64}", head)):
        raise HTTPException(503, detail={"error": "Guest preview snapshot is unavailable"})
    return client, source, head


def _size(metadata):
    value = metadata.get("size_bytes")
    return value if type(value) is int and value >= 0 else None


def _kind(path, size, lfs=False):
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
    return bool((record is not None and record.lfs) or lfs_oid(metadata.get("physical_address")))


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
    path = _parameters(request, root=True)
    _headers(response)
    client, source, head = await _snapshot(repo)
    prefix = path + "/" if path else ""
    if head is None:
        payload = {"entries": [], "path": path, "has_more": False}
    else:
        listing = await _storage(client.list_objects(repository=source, ref=head, prefix=prefix, delimiter="/", amount=MAX_TREE_ENTRIES))
        objects = listing.get("results", [])
        if not isinstance(objects, list):
            raise HTTPException(503, detail={"error": "Guest preview listing is unavailable"})
        candidates = []
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
            relative = entry_path[len(prefix):]
            if not relative or "/" in relative:
                continue  # An ignored delimiter never turns this into a recursive listing.
            candidates.append((entry_path, directory, obj))
        records = {record.path_in_repo: record for record in File.select().where(
            (File.repository == repo.id) & (File.branch == "main") & File.path_in_repo.in_([path for path, directory, _ in candidates if not directory])
        )}
        entries, seen = [], set()
        for entry_path, directory, obj in candidates:
            record = records.get(entry_path)
            if entry_path in seen or (record is not None and record.is_deleted):
                continue
            seen.add(entry_path)
            size = 0 if directory else _size(obj)
            entries.append({"path": entry_path, "type": "directory" if directory else "file", "size": size,
                            "preview_kind": None if directory else _kind(entry_path, size, _is_lfs(obj, record))})
        more = len(objects) > MAX_TREE_ENTRIES or bool(listing.get("pagination", {}).get("has_more", False))
        payload = {"entries": entries, "path": path, "has_more": more}
        if more:
            payload["message"] = "Guest browsing shows at most the first 100 entries in each directory. Further entries require an authorized account."
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
    if kind != "unsupported":
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
