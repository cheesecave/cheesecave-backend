"""Homepage API checks use the shared real-database fixture and real admin authentication."""

from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi import FastAPI
from fastapi.testclient import TestClient
from peewee import OperationalError
import pytest

from kohakuhub import db as db_module, site_homepage
from kohakuhub.api.admin.routers.site_homepage import router as admin_router
from kohakuhub.api.site_homepage import router as public_router
from kohakuhub.config import cfg
from kohakuhub.db import SiteHomepage

PUBLIC_URL = "/api/site-homepage"
ADMIN_URL = "/admin/api/site-homepage"
TOKEN = "homepage-regression-token"
HEADERS = {"X-Admin-Token": TOKEN}


@pytest.fixture
def homepage_client(db_fresh, monkeypatch):
    # Requests run on TestClient's worker threads, so the rows must be committed:
    # db_fresh is a file-backed database, not a rolled-back transaction.
    monkeypatch.setattr(cfg.admin, "enabled", True)
    monkeypatch.setattr(cfg.admin, "secret_token", TOKEN)
    app = FastAPI()
    app.include_router(public_router, prefix="/api")
    app.include_router(admin_router, prefix="/admin/api")
    with TestClient(app, raise_server_exceptions=False) as session:
        yield session, db_fresh


def test_defaults_and_partial_edits_persist_after_restart(homepage_client):
    session, database = homepage_client
    response = session.get(PUBLIC_URL)
    expected = site_homepage.default_homepage()
    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["Cache-Control"] == "no-store"
    assert SiteHomepage.select().count() == 0
    first = session.put(ADMIN_URL, headers=HEADERS, json={"title": "  A community hub  "})
    assert first.status_code == 200
    expected.update(title="A community hub")
    patch = {"animation_enabled": False, "primary_label": "", "primary_url": "https://example.com"}
    assert session.put(ADMIN_URL, headers=HEADERS, json=patch).status_code == 200
    expected.update(patch)
    # Close and reopen the file: the saved configuration is on disk, not in memory.
    database.close()
    database.connect()
    assert site_homepage.get_homepage() == expected
    assert session.get(PUBLIC_URL).json() == expected
    assert session.get(ADMIN_URL, headers=HEADERS).json() == expected
    assert SiteHomepage.select().count() == 1
    assert session.put(ADMIN_URL, headers=HEADERS, json={}).json() == expected


@pytest.mark.parametrize("method", ["get", "put"])
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
def test_admin_requires_valid_token(homepage_client, method, headers):
    session, _ = homepage_client
    kwargs = {"json": {"title": "Unauthorized"}} if method == "put" else {}
    assert getattr(session, method)(ADMIN_URL, headers=headers, **kwargs).status_code in {401, 403}
    assert SiteHomepage.select().count() == 0


def test_disabled_admin_is_rejected(homepage_client, monkeypatch):
    session, _ = homepage_client
    monkeypatch.setattr(cfg.admin, "enabled", False)
    assert session.get(ADMIN_URL, headers=HEADERS).status_code == 503
    assert session.get(PUBLIC_URL).status_code == 200


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "true"},
        {"animation_enabled": 0},
        {"show_repositories": None},
        {"title": "   "},
        {"title": None},
        {"title": 5},
        {"title": "x" * 201},
        {"eyebrow": "x" * 101},
        {"description": "x" * 2001},
        {"primary_label": "x" * 81},
        {"secondary_label": None},
        {"primary_url": "x" * 2049},
        {"illustration": "custom-svg"},
        {"unknown": "value"},
    ],
)
def test_invalid_configuration_does_not_modify_storage(homepage_client, patch):
    session, _ = homepage_client
    assert session.put(ADMIN_URL, headers=HEADERS, json=patch).status_code == 422
    assert SiteHomepage.select().count() == 0


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "//example.com",
        "relative/path",
        "\\example.com",
        "/\\example.com",
        "/path\n",
        "/path\x00",
        "/path\x7f",
        " https://example.com",
        "https://example.com/has space",
        "https://",
        "http://[broken",
        "https://example.com:99999",
        "ftp://example.com",
        "https:example.com",
        "https://user@example.com",
        "https://user:password@example.com",
    ],
)
@pytest.mark.parametrize("field", ["primary_url", "secondary_url"])
def test_unsafe_links_are_rejected(homepage_client, url, field):
    session, _ = homepage_client
    response = session.put(ADMIN_URL, headers=HEADERS, json={field: url})
    assert response.status_code == 422
    assert SiteHomepage.select().count() == 0


@pytest.mark.parametrize(
    "url", ["/", "/models?sort=likes#top", "https://example.com/docs", "http://localhost:8080", ""]
)
def test_safe_links_and_hidden_actions(homepage_client, url):
    session, _ = homepage_client
    response = session.put(
        ADMIN_URL, headers=HEADERS, json={"primary_url": url, "primary_label": "   "}
    )
    assert response.status_code == 200
    assert response.json()["primary_url"] == url
    assert response.json()["primary_label"] == ""


def test_database_outage_uses_public_defaults_but_admin_reports_failure(
    homepage_client, monkeypatch
):
    # Deliberate outage: the request runs on a worker thread, where a rolled-back
    # DROP TABLE would not be visible, so the failing read is injected here.
    session, _ = homepage_client
    monkeypatch.setattr(SiteHomepage, "get_or_none", Mock(side_effect=OperationalError("offline")))
    response = session.get(PUBLIC_URL)
    assert response.status_code == 200
    assert response.json() == site_homepage.default_homepage()
    assert response.headers["X-Site-Homepage-Fallback"] == "true"
    assert session.get(ADMIN_URL, headers=HEADERS).status_code == 500


def test_invalid_stored_links_never_reach_the_public_page(homepage_client):
    session, _ = homepage_client
    SiteHomepage.create(id=1, primary_url="javascript:alert(1)")
    response = session.get(PUBLIC_URL)
    assert response.json() == site_homepage.default_homepage()
    assert response.headers["X-Site-Homepage-Fallback"] == "true"


def test_invalid_existing_configuration_rolls_back_admin_partial_save(homepage_client):
    session, _ = homepage_client
    SiteHomepage.create(id=1, title="", enabled=True, description="Keep this")
    response = session.put(
        ADMIN_URL, headers=HEADERS, json={"enabled": False, "description": "Changed"}
    )
    assert response.status_code == 500
    record = SiteHomepage.get_by_id(1)
    assert record.enabled is True and record.description == "Keep this" and record.title == ""
    # The same API can repair the corrupt field in an otherwise valid patch.
    assert session.put(ADMIN_URL, headers=HEADERS, json={"title": "Repaired"}).status_code == 200


def test_concurrent_homepage_partial_updates_keep_both_fields(homepage_client):
    _, database = homepage_client
    barrier = Barrier(2)

    def save(patch):
        with database.connection_context():
            barrier.wait(timeout=5)
            return site_homepage.update_homepage(patch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(save, patch) for patch in ({"enabled": False}, {"title": "Both saved"})
        ]
        for future in futures:
            future.result(timeout=10)
    assert site_homepage.get_homepage()["enabled"] is False
    assert site_homepage.get_homepage()["title"] == "Both saved"


def test_init_db_creates_homepage_table(monkeypatch):
    database = Mock()
    monkeypatch.setattr(db_module, "db", database)
    db_module.init_db()
    assert SiteHomepage in database.create_tables.call_args.args[0]
