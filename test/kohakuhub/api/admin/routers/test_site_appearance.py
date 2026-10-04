"""Appearance settings regression checks with real admin auth and isolated SQLite."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from peewee import OperationalError, SqliteDatabase
import pytest

from kohakuhub import db as db_module, site_appearance
from kohakuhub.api.admin.routers.site_appearance import router as admin_router
from kohakuhub.api.site_appearance import router as public_router
from kohakuhub.config import cfg
from kohakuhub.db import SiteAppearance

ADMIN_URL = "/admin/api/site-appearance"
PUBLIC_URL = "/api/site-appearance"
TOKEN = "appearance-regression-token"
HEADERS = {"X-Admin-Token": TOKEN}


@pytest.fixture
def appearance_client(tmp_path, monkeypatch):
    database = SqliteDatabase(str(tmp_path / "appearance.db"), timeout=10)
    original_database = SiteAppearance._meta.database
    SiteAppearance.bind(database)
    database.create_tables([SiteAppearance])
    monkeypatch.setattr(cfg.admin, "enabled", True)
    monkeypatch.setattr(cfg.admin, "secret_token", TOKEN)
    app = FastAPI()
    app.include_router(public_router, prefix="/api")
    app.include_router(admin_router, prefix="/admin/api")
    try:
        with TestClient(app, raise_server_exceptions=False) as session:
            yield session, database
    finally:
        database.close()
        SiteAppearance.bind(original_database)


def test_public_defaults_do_not_insert_overrides(appearance_client):
    session, _ = appearance_client
    expected = site_appearance.default_appearance()
    assert set(expected) == {"footer", "theme"}
    assert expected["theme"] == {
        "default_mode": "system",
        "primary_light": "#94621f",
        "primary_dark": "#e6b85c",
        "background_light": "#f7f4eb",
        "background_dark": "#1c211d",
        "card_light": "#fffdf7",
        "card_dark": "#282e27",
    }
    assert [group["title"] for group in expected["footer"]["groups"]] == [
        "Using this hub",
        "Open source",
        "Policies",
    ]
    assert "footer_description" not in expected["footer"]
    assert set(expected["footer"]) == {"groups", "show_build_info"}
    response = session.get(PUBLIC_URL)
    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["Cache-Control"] == "no-store"
    assert "X-Site-Appearance-Fallback" not in response.headers
    assert SiteAppearance.select().count() == 0
    assert (
        session.put(ADMIN_URL, headers=HEADERS, json={"footer": {}, "theme": {}}).json() == expected
    )
    assert SiteAppearance.select().count() == 0


def test_partial_nested_updates_persist_without_resetting_unsupplied_fields(appearance_client):
    session, database = appearance_client
    first_patch = {
        "footer": {"groups": [{"title": "Custom links", "links": []}], "show_build_info": False},
        "theme": {"primary_light": "#123456"},
    }
    response = session.put(ADMIN_URL, headers=HEADERS, json=first_patch)
    assert response.status_code == 200
    expected = site_appearance.default_appearance()
    expected["footer"].update(groups=first_patch["footer"]["groups"], show_build_info=False)
    expected["theme"]["primary_light"] = "#123456"
    second_patch = {
        "footer": {"groups": []},
        "theme": {"default_mode": "dark"},
    }
    response = session.put(ADMIN_URL, headers=HEADERS, json=second_patch)
    expected["footer"]["groups"] = []
    expected["theme"]["default_mode"] = "dark"
    assert response.json() == expected
    assert response.headers["Cache-Control"] == "no-store"
    record = SiteAppearance.get_by_id(1)
    assert json.loads(record.theme) == {"primary_light": "#123456", "default_mode": "dark"}
    database.close()
    restarted = SqliteDatabase(database.database)
    with SiteAppearance.bind_ctx(restarted), restarted.connection_context():
        assert site_appearance.get_appearance() == expected
    assert session.get(PUBLIC_URL).json() == expected
    admin_response = session.get(ADMIN_URL, headers=HEADERS)
    assert admin_response.json() == expected
    assert admin_response.headers["Cache-Control"] == "no-store"
    assert SiteAppearance.select().count() == 1


def test_groups_replace_as_a_whole_and_can_be_hidden(appearance_client):
    session, _ = appearance_client
    groups = [{"title": "  Community  ", "links": [{"label": "  Models  ", "url": "/models"}]}]
    response = session.put(ADMIN_URL, headers=HEADERS, json={"footer": {"groups": groups}})
    assert response.status_code == 200
    assert response.json()["footer"]["groups"] == [
        {"title": "Community", "links": [{"label": "Models", "url": "/models"}]},
    ]
    assert (
        session.put(ADMIN_URL, headers=HEADERS, json={"footer": {"groups": []}}).json()["footer"][
            "groups"
        ]
        == []
    )


def test_color_values_are_normalized_to_match_frontend_contract(appearance_client):
    session, _ = appearance_client
    response = session.put(ADMIN_URL, headers=HEADERS, json={"theme": {"primary_light": "#ABCDEF"}})
    assert response.status_code == 200
    assert response.json()["theme"]["primary_light"] == "#abcdef"


def test_saved_theme_colors_survive_default_palette_change(appearance_client):
    session, _ = appearance_client
    saved_theme = {
        "default_mode": "light",
        "primary_light": "#2563eb",
        "primary_dark": "#3b82f6",
        "background_light": "#f9fafb",
        "background_dark": "#111827",
        "card_light": "#ffffff",
        "card_dark": "#1f2937",
    }
    stored_json = json.dumps(saved_theme)
    SiteAppearance.create(id=1, theme=stored_json)
    assert session.get(PUBLIC_URL).json()["theme"] == saved_theme
    assert SiteAppearance.get_by_id(1).theme == stored_json
    response = session.put(ADMIN_URL, headers=HEADERS, json={"footer": {"show_build_info": False}})
    assert response.status_code == 200
    assert response.json()["theme"] == saved_theme
    assert SiteAppearance.get_by_id(1).theme == stored_json


@pytest.mark.parametrize("method", ["get", "put"])
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
def test_admin_authentication_is_required(appearance_client, method, headers):
    session, _ = appearance_client
    kwargs = {"json": {"theme": {"default_mode": "dark"}}} if method == "put" else {}
    assert getattr(session, method)(ADMIN_URL, headers=headers, **kwargs).status_code in {401, 403}
    assert SiteAppearance.select().count() == 0


def test_disabled_admin_does_not_disable_public_config(appearance_client, monkeypatch):
    session, _ = appearance_client
    monkeypatch.setattr(cfg.admin, "enabled", False)
    assert session.get(ADMIN_URL, headers=HEADERS).status_code == 503
    assert session.get(PUBLIC_URL).status_code == 200


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": "value"},
        {"footer": None},
        {"theme": None},
        {"footer": {"unknown": "value"}},
        {"theme": {"unknown": "value"}},
        {"footer": {"footer_description": "Owned by branding"}},
        {"footer": {"show_build_info": "true"}},
        {"footer": {"show_build_info": 1}},
        {"footer": {"groups": [{"title": "x" * 101, "links": []}]}},
        {"footer": {"groups": [{"title": "Group", "links": [{"label": "x" * 101, "url": "/"}]}]}},
        {
            "footer": {
                "groups": [
                    {"title": "Group", "links": [{"label": "Link", "url": "/" + "x" * 2048}]}
                ]
            }
        },
        {"footer": {"groups": [{"title": "Group", "links": []}] * 4}},
        {"footer": {"groups": [{"title": "Group", "links": [{"label": "x", "url": "/"}] * 9}]}},
        {"footer": {"groups": [{"title": " ", "links": []}]}},
        {"footer": {"groups": [{"title": "Group", "links": [{"label": "", "url": "/"}]}]}},
        {"footer": {"groups": [{"title": "Group", "links": [{"label": "Link", "url": ""}]}]}},
        {
            "footer": {
                "groups": [{"title": "Group", "links": [{"label": "Link", "url": "/", "extra": 1}]}]
            }
        },
        {"footer": {"groups": [{"title": "Group", "links": [], "extra": 1}]}},
        {"theme": {"default_mode": "auto"}},
        {"theme": {"default_mode": None}},
        {"theme": {"primary_light": "red"}},
        {"theme": {"primary_light": "#fff"}},
        {"theme": {"background_dark": "#123456;display:none"}},
        {"theme": {"card_light": "#12345g"}},
        {"theme": {"primary_dark": 123456}},
    ],
)
def test_invalid_values_are_rejected_without_saving(appearance_client, patch):
    session, _ = appearance_client
    assert session.put(ADMIN_URL, headers=HEADERS, json=patch).status_code == 422
    assert SiteAppearance.select().count() == 0


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//example.com",
        "relative",
        "/\\evil",
        "/path\n",
        "/path\x00",
        "/path\x7f",
        "/path\x85",
        " https://example.com",
        "https://example.com/has space",
        "https:example.com",
        "https://user:pass@example.com",
        "https://",
        "http://[broken",
        "https://example.com:99999",
    ],
)
def test_unsafe_group_links_are_rejected(appearance_client, url):
    session, _ = appearance_client
    value = [{"title": "Group", "links": [{"label": "Link", "url": url}]}]
    assert (
        session.put(ADMIN_URL, headers=HEADERS, json={"footer": {"groups": value}}).status_code
        == 422
    )
    assert SiteAppearance.select().count() == 0


@pytest.mark.parametrize("field", sorted(site_appearance.LEGACY_CREDIT_FIELDS))
def test_protected_credits_cannot_be_updated_explicitly(appearance_client, field):
    session, _ = appearance_client
    response = session.put(
        ADMIN_URL,
        headers=HEADERS,
        json={
            "footer": {field: "Attempted override", "show_build_info": False},
            "theme": {"primary_light": "#654321"},
        },
    )
    assert response.status_code == 422
    assert SiteAppearance.select().count() == 0


def test_legacy_credit_overrides_are_ignored_and_cleaned_on_footer_save(appearance_client):
    session, _ = appearance_client
    legacy = {
        field: {"invalid": "Old protected values are not consumed"}
        for field in site_appearance.LEGACY_CREDIT_FIELDS
    }
    groups = [{"title": "Kept group", "links": [{"label": "Kept link", "url": "/models"}]}]
    SiteAppearance.create(
        id=1,
        footer=json.dumps({**legacy, "groups": groups, "show_build_info": False}),
        theme='{"default_mode":"dark","primary_light":"#654321"}',
    )
    expected = site_appearance.default_appearance()
    expected["footer"].update(groups=groups, show_build_info=False)
    expected["theme"].update(default_mode="dark", primary_light="#654321")
    for url, headers in [(PUBLIC_URL, {}), (ADMIN_URL, HEADERS)]:
        response = session.get(url, headers=headers)
        assert response.status_code == 200
        assert response.json() == expected
        assert "X-Site-Appearance-Fallback" not in response.headers
    response = session.put(ADMIN_URL, headers=HEADERS, json={"footer": {"show_build_info": True}})
    expected["footer"]["show_build_info"] = True
    assert response.status_code == 200
    assert response.json() == expected
    assert json.loads(SiteAppearance.get_by_id(1).footer) == {
        "groups": groups,
        "show_build_info": True,
    }


def test_database_outage_returns_public_defaults_and_admin_errors(appearance_client, monkeypatch):
    session, _ = appearance_client
    monkeypatch.setattr(
        SiteAppearance, "get_or_none", Mock(side_effect=OperationalError("offline"))
    )
    response = session.get(PUBLIC_URL)
    assert response.status_code == 200
    assert response.json() == site_appearance.default_appearance()
    assert response.headers["X-Site-Appearance-Fallback"] == "true"
    assert session.get(ADMIN_URL, headers=HEADERS).status_code == 500


@pytest.mark.parametrize(
    "field,value",
    [
        ("footer", "broken json"),
        ("footer", "[]"),
        (
            "footer",
            '{"groups":[{"title":"Group","links":[{"label":"Link","url":"javascript:alert(1)"}]}]}',
        ),
        ("footer", '{"unknown":"Still rejected"}'),
        ("theme", '{"primary_light":"red"}'),
    ],
)
def test_invalid_stored_configuration_uses_safe_public_defaults(appearance_client, field, value):
    session, _ = appearance_client
    SiteAppearance.create(id=1, **{field: value})
    response = session.get(PUBLIC_URL)
    assert response.json() == site_appearance.default_appearance()
    assert response.headers["X-Site-Appearance-Fallback"] == "true"


def test_partial_write_rolls_back_when_stored_other_section_is_invalid(appearance_client):
    session, _ = appearance_client
    SiteAppearance.create(id=1, theme='{"primary_light":"red"}')
    assert (
        session.put(
            ADMIN_URL, headers=HEADERS, json={"footer": {"show_build_info": False}}
        ).status_code
        == 500
    )
    assert SiteAppearance.get_by_id(1).footer is None


def test_concurrent_nested_updates_preserve_other_fields_and_sections(appearance_client):
    _, database = appearance_client
    patches = [
        {"footer": {"groups": [{"title": "First writer", "links": []}]}},
        {"footer": {"show_build_info": False}},
        {"theme": {"default_mode": "dark"}},
        {"theme": {"primary_light": "#654321"}},
    ]
    barrier = Barrier(len(patches))

    def save(patch):
        with database.connection_context():
            barrier.wait(timeout=10)
            site_appearance.update_appearance(patch)

    with ThreadPoolExecutor(max_workers=len(patches)) as pool:
        list(pool.map(save, patches))
    saved = site_appearance.get_appearance()
    assert saved["footer"]["groups"] == [{"title": "First writer", "links": []}]
    assert saved["footer"]["show_build_info"] is False
    assert saved["theme"]["default_mode"] == "dark"
    assert saved["theme"]["primary_light"] == "#654321"
    assert SiteAppearance.select().count() == 1


def test_init_db_creates_appearance_table(monkeypatch):
    database = Mock()
    monkeypatch.setattr(db_module, "db", database)
    db_module.init_db()
    assert SiteAppearance in database.create_tables.call_args.args[0]
