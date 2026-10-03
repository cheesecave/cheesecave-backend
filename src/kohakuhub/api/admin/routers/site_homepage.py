"""Admin management of the public homepage card."""

from fastapi import APIRouter, Depends

from kohakuhub.api.admin.utils.auth import verify_admin_token
from kohakuhub.site_homepage import HomepagePatch, get_homepage, update_homepage

router = APIRouter(prefix="/site-homepage", dependencies=[Depends(verify_admin_token)])


@router.get("")
def read_site_homepage():
    return get_homepage()


@router.put("")
def edit_site_homepage(body: HomepagePatch):
    return update_homepage(body.model_dump(exclude_unset=True))
