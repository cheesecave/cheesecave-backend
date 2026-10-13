"""``Link: <url>; rel="next"`` (RFC 8288), how the Hub pages a bare JSON array."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request

from kohakuhub.config import cfg


def next_link(request: Request, cursor: str) -> str:
    """Absolute URL of the same request with ``cursor`` replaced (repeated parameters kept)."""
    kept = [(n, v) for n, v in request.query_params.multi_items() if n != "cursor"]
    kept.append(("cursor", cursor))
    return f"{cfg.app.base_url.rstrip('/')}{request.url.path}?{urlencode(kept)}"


def link_header(request: Request, cursor: str | None) -> dict[str, str]:
    """The response headers for a page whose next page starts at ``cursor`` (none on the last page)."""
    return {"Link": f'<{next_link(request, cursor)}>; rel="next"'} if cursor else {}
