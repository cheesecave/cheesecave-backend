"""Persistent homepage settings shared by the public page and admin editor."""

from typing import Literal
from urllib.parse import urlsplit

from peewee import DatabaseError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError
from pydantic import field_validator

from kohakuhub.db import SiteHomepage
from kohakuhub.logger import get_logger

logger = get_logger("SITE_HOMEPAGE")


def validate_cta_url(value: str) -> str:
    """Allow local paths and absolute HTTP(S) destinations, never executable URLs."""
    if (
        any(
            character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
            for character in value
        )
        or "\\" in value
    ):
        raise ValueError("Links cannot contain whitespace, control characters, or backslashes")
    if not value:
        return value
    if value.startswith("/") and not value.startswith("//"):
        return value
    try:
        parsed = urlsplit(value)
        if (
            value.lower().startswith(("http://", "https://"))
            and parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            # Accessing port validates malformed and out-of-range port numbers.
            parsed.port
            return value
    except ValueError:
        pass
    raise ValueError("Use a root-relative path or an absolute HTTP(S) URL")


class HomepageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = True
    eyebrow: StrictStr = Field(default="THE HOME FOR YOUR AI PROJECTS", max_length=100)
    title: StrictStr = Field(default="Your next idea starts here.", max_length=200)
    description: StrictStr = Field(
        default="Discover, share, and build with models, datasets, and spaces. "
        "A home for your work and your community.",
        max_length=2000,
    )
    primary_label: StrictStr = Field(default="Get Started", max_length=80)
    primary_url: StrictStr = Field(default="/get-started", max_length=2048)
    secondary_label: StrictStr = Field(default="Host Your Own Hub", max_length=80)
    secondary_url: StrictStr = Field(default="/self-hosted", max_length=2048)
    illustration: Literal["mouse-cheese", "none"] = "mouse-cheese"
    animation_enabled: StrictBool = True
    show_repositories: StrictBool = True

    @field_validator("title")
    @classmethod
    def validate_title(cls, value):
        if not value.strip():
            raise ValueError("Title cannot be blank")
        return value.strip()

    @field_validator("eyebrow", "primary_label", "secondary_label")
    @classmethod
    def trim_label(cls, value):
        return value.strip()

    @field_validator("primary_url", "secondary_url")
    @classmethod
    def validate_url(cls, value):
        return validate_cta_url(value)


class HomepagePatch(HomepageConfig):
    """The defaults make every field optional; only supplied fields are persisted."""


def default_homepage() -> dict:
    return HomepageConfig().model_dump()


def get_homepage() -> dict:
    values = default_homepage()
    record = SiteHomepage.get_or_none(SiteHomepage.id == 1)
    if record is not None:
        values.update(
            {
                field: getattr(record, field)
                for field in values
                if getattr(record, field) is not None
            }
        )
    # Validate stored values too, preventing legacy/manual DB changes bypassing URL checks.
    return HomepageConfig.model_validate(values).model_dump()


def get_public_homepage() -> tuple[dict, bool]:
    try:
        return get_homepage(), False
    except (DatabaseError, ValidationError):
        logger.warning("Site homepage unavailable or invalid; returning bundled defaults")
        return default_homepage(), True


def update_homepage(values: dict) -> dict:
    # Update individual columns atomically, preserving concurrent edits to other fields.
    if values:
        (
            SiteHomepage.insert(id=1, **values)
            .on_conflict(conflict_target=[SiteHomepage.id], update=values)
            .execute()
        )
    return get_homepage()
