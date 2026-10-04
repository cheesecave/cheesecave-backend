"""Admin management of footer links, attribution and site theme colors."""

from fastapi import APIRouter, Depends, Response

from kohakuhub.api.admin.utils.auth import verify_admin_token
from kohakuhub.site_appearance import AppearancePatch, get_appearance, update_appearance

router = APIRouter(prefix="/site-appearance", dependencies=[Depends(verify_admin_token)])


@router.get("")
def read_site_appearance(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return get_appearance()


@router.put("")
def edit_site_appearance(body: AppearancePatch, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return update_appearance(body.model_dump(exclude_unset=True))
