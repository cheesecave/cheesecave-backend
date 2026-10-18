"""Pytest fixtures for KohakuHub backend tests."""

from __future__ import annotations

import importlib

import httpx
import pytest
import pytest_asyncio

from test.kohakuhub.support.bootstrap import (
    ADMIN_TOKEN,
    DEFAULT_PASSWORD,
    backend_module_snapshot,
    backend_modules_swapped,
)
from test.kohakuhub.support.live_server import start_live_server, stop_live_server
from test.kohakuhub.support.service_bootstrap import apply_service_test_env
from test.kohakuhub.support.service_state import create_service_test_state

apply_service_test_env()

_INITIAL_BASELINE_READY = False
_LAST_BACKEND_MODULE = None
_BACKEND_FIXTURE_NAMES = {
    "prepared_backend_test_state",
    "backend_test_state",
    "app",
    "client",
    "owner_client",
    "member_client",
    "visitor_client",
    "outsider_client",
    "admin_client",
    "live_server_url",
    "hf_api_token",
}
# The kohakuhub modules the test files imported (see _standalone_modules)
_COLLECTED_MODULES = {}


def pytest_collection_finish(session):
    _COLLECTED_MODULES.update(backend_module_snapshot())


@pytest.fixture(autouse=True)
def _forget_admin_throttle():
    """Wrong admin tokens are counted per client; tests must not inherit each other's count."""
    import sys

    def reset():
        module = sys.modules.get("kohakuhub.auth.admin_token")
        if module is not None:
            module.throttle.reset()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _standalone_modules(request):
    """A test without the backend fixtures runs on the kohakuhub modules the
    test files imported. The backend fixtures reload every one of them; code
    that imports inside a function would otherwise get the reloaded copy and
    mix it with the test's (test_backend_module_swap_unit.py)."""
    if _BACKEND_FIXTURE_NAMES.intersection(request.fixturenames):
        yield
        return
    with backend_modules_swapped(_COLLECTED_MODULES):
        yield


@pytest.fixture
def commit_ids_in_full(monkeypatch):
    """The site says commits as LakeFS has them (``commit_id_format = "full"``).

    For tests about what is recorded for a commit (revert, reset, squash, concurrent commits, usage): they hold an
    id an API response said and compare it with the full ids LakeFS and the database hold. The form the API says is
    tested where it is the point (test_commit_id_outbound.py, the huggingface_hub tests).
    """
    import importlib

    monkeypatch.setattr(importlib.import_module("kohakuhub.config").cfg.app, "commit_id_format", "full")


@pytest.fixture(scope="session")
def backend_test_state(pytestconfig):
    terminal_reporter = pytestconfig.pluginmanager.get_plugin("terminalreporter")

    def report_progress(message: str) -> None:
        if terminal_reporter is not None:
            terminal_reporter.write_line(f"[backend-test] {message}")

    return create_service_test_state(progress_callback=report_progress)


@pytest.fixture(scope="session")
async def prepared_backend_test_state(backend_test_state):
    global _INITIAL_BASELINE_READY
    await backend_test_state.prepare()
    _INITIAL_BASELINE_READY = True
    return backend_test_state


@pytest.fixture(scope="session")
def app(prepared_backend_test_state):
    return prepared_backend_test_state.modules.app


@pytest.fixture(autouse=True)
def _restore_backend_state_per_test(request):
    global _INITIAL_BASELINE_READY, _LAST_BACKEND_MODULE

    needs_backend = bool(_BACKEND_FIXTURE_NAMES.intersection(request.fixturenames))
    if not needs_backend:
        return

    backend_test_state = request.getfixturevalue("backend_test_state")
    request.getfixturevalue("prepared_backend_test_state")
    current_module = request.node.module.__name__

    # Drop the LakeFSRestClient singleton's pooled httpx client between
    # tests. ``httpx.AsyncClient`` binds its connection pool to the event
    # loop it was constructed in. Two loops are in play:
    #
    #   * ``restore_active_state()`` uses ``asyncio.run(...)`` internally —
    #     a short-lived loop for the clear+seed phase. FastAPI handlers
    #     driven during the seed lazily build the pool *inside* that loop,
    #     then the loop closes.
    #   * pytest-asyncio gives each test its own fresh loop.
    #
    # We null the singleton both *before* the restore (so the seed phase
    # doesn't inherit a pool tied to the previous test's closed loop) and
    # again *after* (so the test body doesn't inherit the seed phase's
    # pool tied to its now-closed asyncio.run loop). Without the second
    # reset, handlers raise ``Event loop is closed`` on the first LakeFS
    # call.
    from kohakuhub import lakefs_rest_client as _lakefs_rest
    _lakefs_rest._singleton_client = None

    if request.node.get_closest_marker("backend_per_test") is not None:
        backend_test_state.restore_active_state()
        _lakefs_rest._singleton_client = None
        _LAST_BACKEND_MODULE = current_module
        # A marked test leaves its changes behind; the next module must
        # restore instead of taking the session's initial baseline as clean.
        _INITIAL_BASELINE_READY = False
        return

    if _LAST_BACKEND_MODULE != current_module:
        if _INITIAL_BASELINE_READY:
            _INITIAL_BASELINE_READY = False
        else:
            backend_test_state.restore_active_state()
            _lakefs_rest._singleton_client = None
        _LAST_BACKEND_MODULE = current_module


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def owner_client(client):
    response = await client.post(
        "/api/auth/login",
        json={"username": "owner", "password": DEFAULT_PASSWORD},
    )
    response.raise_for_status()
    return client


@pytest_asyncio.fixture
async def member_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as async_client:
        response = await async_client.post(
            "/api/auth/login",
            json={"username": "member", "password": DEFAULT_PASSWORD},
        )
        response.raise_for_status()
        yield async_client


@pytest_asyncio.fixture
async def visitor_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as async_client:
        response = await async_client.post(
            "/api/auth/login",
            json={"username": "visitor", "password": DEFAULT_PASSWORD},
        )
        response.raise_for_status()
        yield async_client


@pytest_asyncio.fixture
async def outsider_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as async_client:
        response = await async_client.post(
            "/api/auth/login",
            json={"username": "outsider", "password": DEFAULT_PASSWORD},
        )
        response.raise_for_status()
        yield async_client


@pytest_asyncio.fixture
async def admin_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
        headers={"X-Admin-Token": ADMIN_TOKEN},
    ) as async_client:
        yield async_client


@pytest.fixture
def live_server_url(app, backend_test_state, monkeypatch):
    # The server runs in a thread with an event loop of its own, and the test's loop (the in-process app) uses the same pooled
    # LakeFS client: a keep-alive connection one loop opened is "bound to a different event loop" for the other, and the request
    # that picks it fails with a 500. A process has one loop in production; here no idle connection is kept to be shared.
    rest = importlib.import_module("kohakuhub.lakefs_rest_client")
    monkeypatch.setattr(
        rest,
        "_HTTPX_LIMITS",
        httpx.Limits(max_connections=rest._HTTPX_LIMITS.max_connections, max_keepalive_connections=0),
    )
    pooled = getattr(rest, "_singleton_client", None)
    if pooled is not None:
        pooled._httpx_client = None  # built again with the limits above, by whichever loop asks first
    cfg = backend_test_state.modules.config_module.cfg
    previous_base_url = cfg.app.base_url
    previous_internal_base_url = cfg.app.internal_base_url
    handle = start_live_server(app)
    cfg.app.base_url = handle.base_url
    cfg.app.internal_base_url = handle.base_url
    try:
        yield handle.base_url
    finally:
        cfg.app.base_url = previous_base_url
        cfg.app.internal_base_url = previous_internal_base_url
        stop_live_server(handle)


@pytest_asyncio.fixture
async def hf_api_token(owner_client):
    response = await owner_client.post(
        "/api/auth/tokens/create",
        json={"name": "hf-api-compat"},
    )
    response.raise_for_status()
    return response.json()["token"]


# Real-database fixtures for unit tests (see test/kohakuhub/support/db.py and AGENTS.md).
from test.kohakuhub.support import db as _db  # noqa: E402


@pytest.fixture(scope="module")
def db_module_scope(tmp_path_factory):
    """One database for the module; tables created once and dropped at module end."""
    database, schema = _db.make_database(tmp_path_factory.mktemp("module-db"), name="module")
    with _db.fresh_database(database, _db.MODELS, schema=schema) as scoped:
        yield scoped
    database.close()


@pytest.fixture
def db_scope(db_module_scope):
    """Writes made by one test are rolled back before the next test (same thread only)."""
    with _db.rolled_back(db_module_scope):
        yield db_module_scope


@pytest.fixture
def db_committed(db_module_scope):
    """Shared database whose tables are emptied before each test.

    Rows are really committed, so code running on other threads (HTTP through TestClient)
    sees them. Cheap on Postgres: one TRUNCATE per test instead of a new schema per test.
    """
    _db.clear_tables(db_module_scope)
    yield db_module_scope
    _db.clear_tables(db_module_scope)


@pytest.fixture
def db_fresh(tmp_path):
    """A new database for this test; for code that commits or is driven through HTTP."""
    database, schema = _db.make_database(tmp_path, name="fresh")
    with _db.fresh_database(database, _db.MODELS, schema=schema) as scoped:
        yield scoped
    database.close()


# Parametrized engines. Only a test that requests db_backend (directly or through db_dual)
# is doubled; the other fixtures above follow KOHAKU_HUB_DB_BACKEND and are not parametrized,
# so ~2000 tests keep one case each.
@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def db_backend(request):
    """Each engine in turn. Postgres skips unless KOHAKU_HUB_DATABASE_URL is a PostgreSQL URL."""
    if request.param == "postgres" and not _db.postgres_configured():
        pytest.skip("no Postgres configured in KOHAKU_HUB_DATABASE_URL")
    return request.param


@pytest.fixture
def db_dual(db_backend, tmp_path):
    """A new database on the engine ``db_backend`` names: the test runs on SQLite and PostgreSQL."""
    database, schema = _db.make_database(tmp_path, name="dual", backend=db_backend)
    with _db.fresh_database(database, _db.MODELS, schema=schema) as scoped:
        yield scoped
    database.close()
