"""Public homepage configuration, available without authentication."""

from fastapi import APIRouter, Response

from kohakuhub.site_homepage import get_public_homepage

router = APIRouter()


@router.get("/site-homepage")
def read_site_homepage(response: Response):
    homepage, fallback = get_public_homepage()
    response.headers["Cache-Control"] = "no-store"
    if fallback:
        response.headers["X-Site-Homepage-Fallback"] = "true"
    return homepage
