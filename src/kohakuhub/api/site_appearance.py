"""Public footer and theme settings, available without authentication."""

from fastapi import APIRouter, Response

from kohakuhub.site_appearance import get_public_appearance

router = APIRouter()


@router.get("/site-appearance")
def read_site_appearance(response: Response):
    appearance, fallback = get_public_appearance()
    response.headers["Cache-Control"] = "no-store"
    if fallback:
        response.headers["X-Site-Appearance-Fallback"] = "true"
    return appearance
