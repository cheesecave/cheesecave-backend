"""Public branding configuration, independent of authentication and storage services."""

from fastapi import APIRouter, Response

from kohakuhub.site_branding import get_public_branding

router = APIRouter()


@router.get("/site-branding")
def read_site_branding(response: Response):
    branding, fallback = get_public_branding()
    # Never let a database outage replace a browser's last successful identity.
    if fallback:
        response.headers["X-Site-Branding-Fallback"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return branding
