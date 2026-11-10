"""Guest-session authenticated PDF page and transcoded media fragment routes."""

import math
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response

from kohakuhub import repository_media as service
from kohakuhub.api import repository_guest_preview as preview
from kohakuhub.api.repository_access import BASE, RepoType

router = APIRouter()


async def _context(repo_type, namespace, name, request, allowed):
    repo = preview._authorize(repo_type, namespace, name, request)
    params = preview._browse_parameters(request, allowed)
    if "page" in allowed:
        _number(params, "page", 1)
    if "segment" in allowed:
        _number(params, "segment", 0)
    path = preview._path(params.get("path", ""))
    snapshot = params.get("snapshot", "")
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot):
        service.fail(400, "Invalid media preview snapshot")
    client, source, head = await preview._snapshot(repo)
    if not head or snapshot != service.snapshot_id(repo, head, path):
        service.fail(409, "Repository changed; reopen the file")
    record = preview._file(repo, path)
    if record is not None and record.is_deleted:
        service.fail(404, "Repository preview not found")
    metadata = await preview._storage(client.stat_object(repository=source, ref=head, path=path, user_metadata=False))
    if not isinstance(metadata, dict) or metadata.get("path", path) != path or metadata.get("path_type", "object") != "object":
        service.fail(404, "Repository preview not found")
    await service.prepare(repo, client, source, head, path, metadata, record)
    return params, snapshot


def _number(params, key, minimum):
    value = params.get(key, "")
    if not re.fullmatch(r"\d{1,6}", value) or int(value) < minimum:
        service.fail(400, "Invalid media preview position")
    return int(value)


async def _response(content, media_type, repo_type, namespace, name, request, params, snapshot):
    repo = preview._authorize(repo_type, namespace, name, request)
    _, _, head = await preview._snapshot(repo)
    if service.snapshot_id(repo, head, params["path"]) != snapshot:
        service.fail(409, "Repository changed; reopen the file")
    preview._authorize(repo_type, namespace, name, request)
    response = Response(content, media_type=media_type)
    preview._headers(response)
    response.headers["Content-Disposition"] = "inline"
    return response


@router.get(BASE + "/guest/media/pdf/page")
async def pdf_page(repo_type: RepoType, namespace: str, name: str, request: Request):
    params, snapshot = await _context(repo_type, namespace, name, request, {"path", "snapshot", "page"})
    page = _number(params, "page", 1)
    raw = await service.page_bytes(snapshot, page)
    return await _response(raw, "image/png", repo_type, namespace, name, request, params, snapshot)


@router.get(BASE + "/guest/media/hls/playlist")
async def hls_playlist(repo_type: RepoType, namespace: str, name: str, request: Request):
    params, snapshot = await _context(repo_type, namespace, name, request, {"path", "snapshot"})
    with service.CACHE.pin(snapshot) as entry:
        if entry.kind not in {"video", "audio"}:
            service.fail(415, "This file has no streaming media preview")
        durations = entry.metadata["durations"]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{math.ceil(max(durations))}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
        for index, duration in enumerate(durations):
            lines.extend([f"#EXTINF:{duration:.6f},", "segment?" + urlencode({"path": params["path"], "snapshot": snapshot, "segment": index})])
        lines.append("#EXT-X-ENDLIST")
    return await _response("\n".join(lines) + "\n", "application/vnd.apple.mpegurl", repo_type, namespace, name, request, params, snapshot)


@router.get(BASE + "/guest/media/hls/segment")
async def hls_segment(repo_type: RepoType, namespace: str, name: str, request: Request):
    params, snapshot = await _context(repo_type, namespace, name, request, {"path", "snapshot", "segment"})
    number = _number(params, "segment", 0)
    raw = service.segment_bytes(snapshot, number)
    return await _response(raw, "video/mp2t", repo_type, namespace, name, request, params, snapshot)
