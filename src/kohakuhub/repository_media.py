"""Private, bounded render cache for browser-only repository media previews.

The cache is never mounted as static files. Sources stay private; callers can
obtain only PDF page rasters or re-encoded HLS segments through an authorized
router. Temporary data is disposable and can be rebuilt after process restart.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlencode

from fastapi import HTTPException
import imageio_ffmpeg

from kohakuhub.config import cfg
from kohakuhub.lfs_gc import lfs_oid

PDF_SUFFIXES = {".pdf"}
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".ogv"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".aiff", ".aif"}
SOURCE_LIMITS = {"pdf": 64 * 1024 * 1024, "video": 256 * 1024 * 1024, "audio": 128 * 1024 * 1024}
OUTPUT_LIMIT = 256 * 1024 * 1024
ASSET_LIMIT = 16 * 1024 * 1024
MAX_DURATION = 600
MAX_CACHE_ENTRIES = 4
CACHE_TTL = 900
RENDER_VERSION = "pdf-hls-v1"


def media_kind(path):
    suffix = Path(path).suffix.lower()
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return None


def snapshot_id(repo, head, path):
    return hashlib.sha256(json.dumps([RENDER_VERSION, repo.id, head, path], separators=(",", ":")).encode()).hexdigest()


def fail(status, message):
    raise HTTPException(status, detail={"error": message})


@dataclass
class Entry:
    directory: Path
    kind: str
    metadata: dict
    accessed: float
    pins: int = 0


class DerivativeCache:
    def __init__(self, root=None):
        self.temporary = tempfile.TemporaryDirectory(prefix="cheesecave-private-media-") if root is None else None
        self.root = Path(root or self.temporary.name)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.entries = {}
        self.building = set()
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)

    def remove(self, directory):
        target, root = Path(directory).resolve(), self.root.resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError("Media cleanup target must be a private cache child")
        shutil.rmtree(target, ignore_errors=True)

    def _capacity(self):
        return len(self.entries) + sum(":" not in key for key in self.building)

    def _prune(self, room=0):
        now = time.monotonic()
        for key, entry in sorted(list(self.entries.items()), key=lambda item: item[1].accessed):
            if entry.pins or (now - entry.accessed < CACHE_TTL and self._capacity() + room <= MAX_CACHE_ENTRIES):
                continue
            self.entries.pop(key, None)
            self.remove(entry.directory)

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry and time.monotonic() - entry.accessed < CACHE_TTL:
                entry.accessed = time.monotonic()
                return entry
            self._prune()
            return None

    @contextmanager
    def pin(self, key):
        with self.lock:
            entry = self.get(key)
            if entry is None:
                fail(409, "Preview expired; reopen the file")
            entry.pins += 1
        try:
            yield entry
        finally:
            with self.lock:
                entry.pins -= 1

    def reserve(self, key):
        with self.lock:
            room = int(":" not in key)
            self._prune(room)
            if self._capacity() + room > MAX_CACHE_ENTRIES:
                raise HTTPException(429, detail={"error": "Preview capacity is busy; retry shortly"}, headers={"Retry-After": "1"})
            if key in self.building or not self.slots.acquire(blocking=False):
                raise HTTPException(429, detail={"error": "Preview is being prepared; retry shortly"}, headers={"Retry-After": "1"})
            self.building.add(key)

    def publish(self, key, entry):
        with self.lock:
            self.building.discard(key)
            self.entries[key] = entry

    def release(self, key):
        with self.lock:
            self.building.discard(key)
            self.slots.release()


CACHE = DerivativeCache()


def _run(args, directory, timeout):
    """Kill the actual native child on timeout/oversize, not just an awaiter."""
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        fail(503, "Media renderer is unavailable")
    started = time.monotonic()
    try:
        while process.poll() is None:
            output = sum(path.stat().st_size for path in directory.iterdir() if path.is_file() and path.name != "source.bin")
            if output > OUTPUT_LIMIT:
                fail(413, "Rendered preview exceeds its processing limit")
            if time.monotonic() - started > timeout:
                fail(504, "Preview processing timed out")
            time.sleep(0.05)
        if process.returncode:
            fail(422, "This file could not be decoded for preview")
        if sum(path.stat().st_size for path in directory.iterdir() if path.is_file() and path.name != "source.bin") > OUTPUT_LIMIT:
            fail(413, "Rendered preview exceeds its processing limit")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def _pdf(directory, operation="pdf-info", page=0):
    worker = Path(__file__).with_name("media_worker.py").resolve()
    _run([sys.executable, str(worker), operation, str(directory), str(page)], directory, 20)


def _hls(directory, kind):
    try:
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (RuntimeError, OSError):
        fail(503, "Media encoder is unavailable")
    args = [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-max_alloc", "268435456", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov,matroska,webm,avi,mpeg,mpegts,flv,asf,ogg,mp3,wav,flac,aac,aiff",
            "-max_pixels", "16777216", "-threads", "2", "-filter_threads", "1", "-i", str(directory / "source.bin"),
            "-t", str(MAX_DURATION + 1), "-map_metadata", "-1", "-map_chapters", "-1"]
    if kind == "video":
        args += ["-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-threads", "2",
                 "-force_key_frames", "expr:gte(t,n_forced*6)"]
    else:
        args += ["-map", "0:a:0", "-vn"]
    args += ["-c:a", "aac", "-b:a", "128k", "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
             "-hls_playlist_type", "vod", "-hls_segment_type", "mpegts", "-hls_segment_filename", str(directory / "segment-%04d.ts"),
             str(directory / "index.m3u8")]
    _run(args, directory, 90)
    durations, files = [], []
    for line in (directory / "index.m3u8").read_text(encoding="utf-8").splitlines():
        if line.startswith("#EXTINF:"):
            value = float(line.split(":", 1)[1].split(",", 1)[0])
            if not math.isfinite(value) or value <= 0 or value > 30:
                fail(422, "Invalid rendered media duration")
            durations.append(value)
        elif line and not line.startswith("#"):
            if not re.fullmatch(r"segment-\d{4}\.ts", line):
                fail(422, "Invalid rendered media segment")
            files.append(line)
    if not durations or len(durations) != len(files) or files != [f"segment-{i:04d}.ts" for i in range(len(files))]:
        fail(422, "Incomplete rendered media preview")
    if sum(durations) > MAX_DURATION + 0.2:
        fail(413, "Media preview supports files up to 10 minutes")
    if any((directory / name).stat().st_size > ASSET_LIMIT for name in files):
        fail(413, "Rendered media segment exceeds its processing limit")
    return {"durations": durations}


def _build(directory, kind):
    if kind == "pdf":
        _pdf(directory)
        return json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    return _hls(directory, kind)


async def _thread(function, *args):
    # A disconnected browser must not release native-processing capacity while
    # its child is still running. The worker itself always has a hard timeout.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _check_source(metadata, record, kind):
    size = metadata.get("size_bytes")
    if type(size) is not int or size < 1:
        fail(422, "Preview needs a file with a known size")
    if size > SOURCE_LIMITS[kind]:
        fail(413, "This file exceeds the media preview size limit")
    address = metadata.get("physical_address")
    if address is not None and not isinstance(address, str):
        fail(503, "Media storage metadata is unavailable")
    oid = lfs_oid(address)
    if oid and (record is None or not record.lfs or record.sha256 != oid or record.is_deleted):
        fail(404, "Repository preview not found")
    return size


async def prepare(repo, client, source, head, path, metadata, record):
    kind = media_kind(path)
    if kind is None:
        fail(415, "This file has no media preview")
    size = _check_source(metadata, record, kind)
    key = snapshot_id(repo, head, path)
    entry = CACHE.get(key)
    if entry:
        return key, entry
    CACHE.reserve(key)
    directory = Path(tempfile.mkdtemp(prefix="render-", dir=CACHE.root))
    try:
        try:
            raw = await asyncio.wait_for(client.get_object_prefix(source, head, path, SOURCE_LIMITS[kind] + 1), timeout=60)
        except Exception:
            fail(503, "Media source is temporarily unavailable")
        if not isinstance(raw, bytes) or len(raw) != size:
            fail(503, "Media source is incomplete or exceeds its size limit")
        if raw.startswith(b"version https://git-lfs.github.com/spec/"):
            fail(422, "An unresolved LFS pointer cannot be previewed")
        oid = lfs_oid(metadata.get("physical_address"))
        if oid and hashlib.sha256(raw).hexdigest() != oid:
            fail(503, "Media source integrity could not be verified")
        (directory / "source.bin").write_bytes(raw)
        del raw
        details = await _thread(_build, directory, kind)
        if kind != "pdf":
            (directory / "source.bin").unlink()
        entry = Entry(directory, kind, details, time.monotonic())
        CACHE.publish(key, entry)
        return key, entry
    except BaseException:
        CACHE.remove(directory)
        raise
    finally:
        CACHE.release(key)


async def describe(repo, client, source, head, path, metadata, record):
    key, entry = await prepare(repo, client, source, head, path, metadata, record)
    if entry.kind == "pdf":
        return {"kind": "pdf", "page_count": entry.metadata["page_count"], "snapshot": key}
    base = f"{cfg.app.api_base}/{repo.repo_type}s/{quote(repo.namespace, safe='')}/{quote(repo.name, safe='')}/access/guest/media/hls/playlist"
    return {"kind": entry.kind, "snapshot": key, "playlist_url": base + "?" + urlencode({"path": path, "snapshot": key}),
            "duration": sum(entry.metadata["durations"])}


async def page_bytes(key, page):
    with CACHE.pin(key) as entry:
        if entry.kind != "pdf" or page < 1 or page > entry.metadata["page_count"]:
            fail(404, "Preview page not found")
        path = entry.directory / f"page-{page:04d}.png"
        if not path.exists():
            operation = key + f":page:{page}"
            CACHE.reserve(operation)
            try:
                await _thread(_pdf, entry.directory, "pdf-page", page)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            finally:
                CACHE.release(operation)
        if path.stat().st_size > ASSET_LIMIT:
            fail(413, "Rendered PDF page exceeds its processing limit")
        return path.read_bytes()


def segment_bytes(key, number):
    with CACHE.pin(key) as entry:
        if entry.kind not in {"video", "audio"} or number < 0 or number >= len(entry.metadata["durations"]):
            fail(404, "Preview segment not found")
        return (entry.directory / f"segment-{number:04d}.ts").read_bytes()
