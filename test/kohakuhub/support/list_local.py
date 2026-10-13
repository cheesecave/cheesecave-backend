"""Local-only listing through the real page assembly, for tests that read the result as items."""

from __future__ import annotations

from kohakuhub.api.repo.routers import info
from kohakuhub.api.repo.utils import listing


async def list_local(repo_type, *, author=None, user=None, limit=None, sort=None):
    query = listing.ListQuery(
        repo_type=repo_type,
        author=author,
        sort=listing.parse_sort(sort),
        page_size=listing.page_size_for(limit),
    )
    page = await listing.build_page(
        query, user, state=None, sources=[], render_local=lambda ids: info._render_local(ids, False)
    )
    return page.items
