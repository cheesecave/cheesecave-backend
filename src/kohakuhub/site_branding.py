"""Persistent site branding and safe, bounded image normalization."""

import base64
from io import BytesIO
from typing import Literal
import warnings

from fastapi import HTTPException
from peewee import DatabaseError
from PIL import Image, ImageOps, UnidentifiedImageError

from kohakuhub.config import cfg
from kohakuhub.db import SiteBranding
from kohakuhub.gif_branding import normalize_gif, set_gif_loop
from kohakuhub.logger import get_logger
from kohakuhub.svg_branding import normalize_svg

logger = get_logger("SITE_BRANDING")

AssetKind = Literal["header_logo", "favicon"]
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_ASSET_BYTES = 256 * 1024
DEFAULT_FOOTER_DESCRIPTION = "Self-hosted HuggingFace Hub alternative"


def default_branding() -> dict:
    return {
        "site_name": cfg.app.site_name,
        "footer_description": DEFAULT_FOOTER_DESCRIPTION,
        "header_logo": None,
        "favicon": None,
    }


def get_branding() -> dict:
    """Read without creating a row, preserving config defaults until overridden."""
    result = default_branding()
    record = SiteBranding.get_or_none(SiteBranding.id == 1)
    if record is not None:
        for field in result:
            value = getattr(record, field)
            if value is not None:
                result[field] = value
    return result


def get_public_branding() -> tuple[dict, bool]:
    """Public pages can still render their bundled defaults if the DB is unavailable."""
    try:
        return get_branding(), False
    except DatabaseError:
        logger.warning("Site branding database unavailable; returning defaults")
        return default_branding(), True


def update_branding(values: dict) -> dict:
    """Update only the supplied columns, so simultaneous asset/text edits are safe."""
    if values:
        (
            SiteBranding.insert(id=1, **values)
            .on_conflict(conflict_target=[SiteBranding.id], update=values)
            .execute()
        )
    return get_branding()


def normalize_asset(contents: bytes, kind: AssetKind, loop: bool = True) -> str:
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, detail="Image must be at most 2 MiB")
    # Detect XML from bytes, independently of the caller-controlled filename/MIME.
    # SVG stays vector data; raster uploads continue to be decoded and re-encoded.
    if contents.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        normalized = normalize_svg(contents)
        if len(normalized) > MAX_ASSET_BYTES:
            raise HTTPException(400, detail="Normalized SVG must be at most 256 KiB")
        return "data:image/svg+xml;base64," + base64.b64encode(normalized).decode("ascii")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(contents)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP", "GIF", "ICO"}:
                    raise HTTPException(400, detail="Use PNG, JPEG, WebP, GIF, ICO, or SVG images")
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(400, detail="Image must be at most 16 million pixels")
                edge = 512 if kind == "header_logo" else 256
                if source.format == "GIF":
                    normalized = normalize_gif(source, edge, loop)
                    mime = "image/gif"
                else:
                    # Other raster formats use their first frame and strip metadata.
                    source.seek(0)
                    image = ImageOps.exif_transpose(source).convert("RGBA")
                    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                    output = BytesIO()
                    image.save(output, format="PNG", optimize=True)
                    normalized = output.getvalue()
                    mime = "image/png"
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise HTTPException(400, detail="Invalid or excessively large raster image") from exc
    if len(normalized) > MAX_ASSET_BYTES:
        raise HTTPException(
            400, detail="Normalized image must be at most 256 KiB; use a simpler image"
        )
    return f"data:{mime};base64," + base64.b64encode(normalized).decode("ascii")


def update_asset_animation(kind: AssetKind, loop: bool) -> dict:
    asset = get_branding()[kind]
    prefix = "data:image/gif;base64,"
    if not asset or not asset.startswith(prefix):
        raise HTTPException(400, detail="Upload a GIF before changing its animation playback")
    try:
        contents = base64.b64decode(asset[len(prefix) :], validate=True)
    except ValueError as exc:
        raise HTTPException(400, detail="Invalid stored GIF animation") from exc
    normalized = set_gif_loop(contents, loop)
    if len(normalized) > MAX_ASSET_BYTES:
        raise HTTPException(400, detail="Normalized GIF must be at most 256 KiB")
    return update_branding({kind: prefix + base64.b64encode(normalized).decode("ascii")})
