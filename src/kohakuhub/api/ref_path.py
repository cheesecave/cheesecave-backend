"""``refs/heads/<branch>`` as a revision (#58).

``huggingface_hub`` quotes a revision with ``safe=""``, so a full ref name arrives as
``.../resolve/refs%2Fheads%2Fdev/<file>``. The ASGI server decodes ``%2F`` before routing
and the route then reads ``refs`` as the revision. The Hub reads that segment as the
branch ``dev`` on ``resolve``, ``revision`` and ``tree``; a literal ``refs/heads/dev`` is
split at its slashes there as it is here.

A branch whose own name has a slash (``refs%2Fheads%2Ffeature%2Fx``) is left as it is:
that is #67.
"""

import re
from urllib.parse import unquote

from starlette.types import ASGIApp, Receive, Scope, Send

_FULL_REF = re.compile(
    rb"^(?P<head>/(?:api/)?(?:(?:models|datasets|spaces)/)?[^/]+/[^/]+/(?:resolve|revision|tree)/)"
    rb"refs%2[fF]heads%2[fF](?P<name>(?:(?!%2[fF])[^/])+)(?=/|$)"
)


def short_ref_path(raw_path: bytes) -> bytes | None:
    """``raw_path`` with a ``refs%2Fheads%2F<name>`` revision segment cut to ``<name>``,
    or ``None`` when there is no such segment."""
    match = _FULL_REF.match(raw_path)
    if match is None:
        return None
    return match["head"] + match["name"] + raw_path[match.end() :]


class FullRefMiddleware:
    """Pure ASGI: routing sees the branch name instead of the full ref name."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and (raw := scope.get("raw_path")):
            short = short_ref_path(raw)
            if short is not None:
                scope = {
                    **scope,
                    "raw_path": short,
                    "path": unquote(short.decode("latin-1")),
                }
        await self.app(scope, receive, send)
