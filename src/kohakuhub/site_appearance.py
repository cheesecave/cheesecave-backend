"""Validated footer and theme overrides, independent of site identity settings."""

import json
from typing import Annotated, Literal

from peewee import DatabaseError, PostgresqlDatabase, SqliteDatabase
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from kohakuhub.db import SiteAppearance
from kohakuhub.logger import get_logger
from kohakuhub.site_homepage import validate_cta_url

logger = get_logger("SITE_APPEARANCE")
PROJECT_URL = "https://github.com/deepghs/KohakuHub"
UPSTREAM_URL = "https://github.com/KohakuBlueleaf/KohakuHub"
LEGACY_CREDIT_FIELDS = frozenset(
    {
        "project_label",
        "project_url",
        "upstream_label",
        "upstream_url",
        "copyright_text",
        "license_label",
        "license_url",
    }
)
Label = Annotated[StrictStr, Field(max_length=100)]
SafeUrl = Annotated[StrictStr, Field(max_length=2048)]
Color = Annotated[StrictStr, Field(pattern=r"^#[0-9a-fA-F]{6}$", min_length=7, max_length=7)]


def default_footer_groups() -> list[dict]:
    return [
        {
            "title": "Using this hub",
            "links": [
                {"label": "Documentation", "url": "/docs"},
                {"label": "Get started", "url": "/get-started"},
                {"label": "About", "url": "/about"},
                {"label": "Self-host", "url": "/self-hosted"},
            ],
        },
        {
            "title": "Open source",
            "links": [
                {"label": "DeepGHS fork", "url": PROJECT_URL},
                {"label": "Upstream project", "url": UPSTREAM_URL},
                {
                    "label": "Report an issue",
                    "url": "https://github.com/cheesecave/cheesecave-web/issues",
                },
                {"label": "Discord", "url": "https://discord.gg/xWYrkyvJ2s"},
            ],
        },
        {
            "title": "Policies",
            "links": [
                {"label": "Terms", "url": "/terms"},
                {"label": "Privacy", "url": "/privacy"},
            ],
        },
    ]


def normalize_url(value: str) -> str:
    return validate_cta_url(value)


class AppearanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class FooterLink(AppearanceModel):
    label: Label
    url: SafeUrl

    @field_validator("label")
    @classmethod
    def nonblank_label(cls, value):
        if not value.strip():
            raise ValueError("Link label cannot be blank")
        return value.strip()

    @field_validator("url")
    @classmethod
    def nonblank_url(cls, value):
        value = normalize_url(value)
        if not value:
            raise ValueError("Link destination cannot be blank")
        return value


class FooterGroup(AppearanceModel):
    title: Label
    links: list[FooterLink] = Field(max_length=8)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value):
        if not value.strip():
            raise ValueError("Group title cannot be blank")
        return value.strip()


class FooterConfig(AppearanceModel):
    groups: list[FooterGroup] = Field(default_factory=default_footer_groups, max_length=3)
    show_build_info: StrictBool = True


class ThemeConfig(AppearanceModel):
    default_mode: Literal["system", "light", "dark"] = "system"
    primary_light: Color = "#94621f"
    primary_dark: Color = "#e6b85c"
    background_light: Color = "#f7f4eb"
    background_dark: Color = "#1c211d"
    card_light: Color = "#fffdf7"
    card_dark: Color = "#282e27"

    @field_validator(
        "primary_light",
        "primary_dark",
        "background_light",
        "background_dark",
        "card_light",
        "card_dark",
    )
    @classmethod
    def normalize_color(cls, value):
        return value.lower()


class AppearanceConfig(AppearanceModel):
    footer: FooterConfig = Field(default_factory=FooterConfig)
    theme: ThemeConfig = Field(default_factory=ThemeConfig)


class AppearancePatch(AppearanceConfig):
    """Only explicitly supplied nested fields are saved; groups replace the entire list."""


def default_appearance() -> dict:
    return AppearanceConfig().model_dump()


def read_overrides(value: str | None, section: str) -> dict:
    overrides = json.loads(value) if value is not None else {}
    if not isinstance(overrides, dict):
        raise ValueError("Appearance overrides must be a JSON object")
    if section == "footer":
        # Older drafts allowed credits to be edited. Ignore only those obsolete
        # keys; unknown fields still fail validation. Fixed attribution is owned
        # by the frontend and never comes from persisted appearance overrides.
        return {key: value for key, value in overrides.items() if key not in LEGACY_CREDIT_FIELDS}
    return overrides


def get_appearance() -> dict:
    values = default_appearance()
    record = SiteAppearance.get_or_none(SiteAppearance.id == 1)
    if record is not None:
        for section in values:
            values[section].update(read_overrides(getattr(record, section), section))
    # Stored values are checked too, preventing manual DB writes from bypassing
    # safe link and CSS color validation.
    return AppearanceConfig.model_validate(values).model_dump()


def get_public_appearance() -> tuple[dict, bool]:
    try:
        return get_appearance(), False
    except (DatabaseError, ValueError, TypeError):
        logger.warning("Site appearance unavailable or invalid; returning bundled defaults")
        return default_appearance(), True


def update_appearance(values: dict) -> dict:
    patch = AppearancePatch.model_validate(values).model_dump(exclude_unset=True)
    patch = {section: fields for section, fields in patch.items() if fields}
    if not patch:
        return get_appearance()
    database = SiteAppearance._meta.database
    # SQLite acquires its write lock before reading. PostgreSQL locks singleton
    # row 1 after INSERT ON CONFLICT resolves concurrent first writes. Merging
    # within the transaction preserves edits to other sections AND nested keys.
    transaction = (
        database.atomic("IMMEDIATE") if isinstance(database, SqliteDatabase) else database.atomic()
    )
    with transaction:
        SiteAppearance.insert(id=1).on_conflict_ignore().execute()
        query = SiteAppearance.select().where(SiteAppearance.id == 1)
        if isinstance(database, PostgresqlDatabase):
            query = query.for_update()
        record = query.get()
        updates = {}
        for section, fields in patch.items():
            overrides = read_overrides(getattr(record, section), section)
            overrides.update(fields)
            updates[section] = json.dumps(overrides, ensure_ascii=False, separators=(",", ":"))
        SiteAppearance.update(**updates).where(SiteAppearance.id == 1).execute()
        # An invalid pre-existing override causes a rollback instead of a false
        # successful save or partial update.
        return get_appearance()
