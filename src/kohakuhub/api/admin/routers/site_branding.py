"""Admin management of site name, footer introduction, and independent logo assets."""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from kohakuhub.api.admin.utils.auth import verify_admin_token
from kohakuhub.site_branding import (
    AssetKind,
    MAX_UPLOAD_BYTES,
    get_branding,
    normalize_asset,
    update_asset_animation,
    update_branding,
)

router = APIRouter(prefix="/site-branding", dependencies=[Depends(verify_admin_token)])


class BrandingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_name: str | None = Field(default=None, max_length=100)
    footer_description: str | None = Field(default=None, max_length=2000)

    @field_validator("site_name", "footer_description", mode="before")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Use a string; null is not a branding override")
        return value

    @field_validator("site_name")
    @classmethod
    def trim_site_name(cls, value):
        if not value.strip():
            raise ValueError("Site name cannot be blank")
        return value.strip()


@router.get("")
def read_site_branding():
    return get_branding()


@router.put("")
def edit_site_branding(body: BrandingPatch):
    return update_branding(body.model_dump(exclude_unset=True))


@router.post("/assets/{kind}")
async def upload_branding_asset(
    kind: AssetKind, file: UploadFile = File(...), loop: bool = Form(True)
):
    try:
        contents = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    return update_branding({kind: normalize_asset(contents, kind, loop)})


class AnimationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loop: StrictBool


@router.patch("/assets/{kind}/animation")
def edit_asset_animation(kind: AssetKind, body: AnimationPatch):
    return update_asset_animation(kind, body.loop)


@router.delete("/assets/{kind}")
def reset_branding_asset(kind: AssetKind):
    return update_branding({kind: None})
