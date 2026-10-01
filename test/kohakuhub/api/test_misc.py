"""API tests for utility routes."""

from types import SimpleNamespace

import httpx
import pytest

import kohakuhub.api.operation_capabilities as operation_capabilities


async def test_version_site_config_and_yaml_validation(client):
    version_response = await client.get("/api/version")
    assert version_response.status_code == 200
    assert version_response.json()["api"] == "kohakuhub"

    site_config_response = await client.get("/api/site-config")
    assert site_config_response.status_code == 200
    assert "site_name" in site_config_response.json()
    # Enabled by default (#99), on PostgreSQL with a supported LakeFS
    assert site_config_response.json()["capabilities"]["repository_operations"] == {
        "revert": True,
        "reset": True,
        "squash": True,
    }

    valid_yaml_response = await client.post(
        "/api/validate-yaml",
        json={"content": "model:\\n  name: demo\\n"},
    )
    assert valid_yaml_response.status_code == 200
    assert valid_yaml_response.json()["valid"] is True

    invalid_yaml_response = await client.post(
        "/api/validate-yaml",
        json={"content": "model: [broken"},
    )
    assert invalid_yaml_response.status_code == 200
    assert invalid_yaml_response.json()["valid"] is False


@pytest.mark.asyncio
async def test_disabled_operations_reject_before_auth_and_repository_lookup(
    app, backend_test_state, client, monkeypatch
):
    active_cfg = backend_test_state.modules.config_module.cfg
    active_branches = backend_test_state.modules.branches_module
    active_repo_crud = backend_test_state.modules.repo_crud_module

    for field in (
        "repository_revert_enabled",
        "repository_reset_enabled",
        "repository_squash_enabled",
    ):
        monkeypatch.setattr(active_cfg.app, field, False)

    auth_calls = []
    repository_lookups = []

    def unexpected_user_dependency():
        auth_calls.append("user")
        return SimpleNamespace(username="owner")

    def unexpected_admin_dependency():
        auth_calls.append("admin")
        return (SimpleNamespace(username="owner"), False)

    def unexpected_repository_lookup(*_args):
        repository_lookups.append(True)
        return SimpleNamespace()

    app.dependency_overrides[active_branches.get_current_user] = unexpected_user_dependency
    app.dependency_overrides[active_repo_crud.get_current_user_or_admin] = (
        unexpected_admin_dependency
    )
    monkeypatch.setattr(active_branches, "get_repository", unexpected_repository_lookup)
    monkeypatch.setattr(active_repo_crud, "get_repository", unexpected_repository_lookup)

    try:
        requests = [
            (
                "/api/models/owner/demo-model/branch/main/revert",
                {"ref": "commit-ref"},
            ),
            (
                "/api/models/owner/demo-model/branch/main/reset",
                {"ref": "commit-ref", "force": True},
            ),
            ("/api/repos/squash", {"repo": "owner/demo-model", "type": "model"}),
        ]
        responses = [await client.post(path, json=payload) for path, payload in requests]
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in responses] == [503, 503, 503]
    assert [response.json()["detail"]["code"] for response in responses] == [
        "operation_disabled",
        "operation_disabled",
        "operation_disabled",
    ]
    assert auth_calls == []
    assert repository_lookups == []


@pytest.mark.asyncio
async def test_enabled_operations_still_require_authentication(
    app, backend_test_state, client, monkeypatch
):
    active_cfg = backend_test_state.modules.config_module.cfg
    active_branches = backend_test_state.modules.branches_module
    active_repo_crud = backend_test_state.modules.repo_crud_module

    monkeypatch.setattr(active_cfg.app, "db_backend", "postgres")
    for field in (
        "repository_revert_enabled",
        "repository_reset_enabled",
        "repository_squash_enabled",
    ):
        monkeypatch.setattr(active_cfg.app, field, True)
    mutation_calls = []

    def unexpected_repository_lookup(*_args):
        mutation_calls.append("repository")
        raise AssertionError("anonymous request reached repository logic")

    monkeypatch.setattr(active_branches, "get_repository", unexpected_repository_lookup)
    monkeypatch.setattr(active_repo_crud, "get_repository", unexpected_repository_lookup)
    app.dependency_overrides[active_branches.require_repository_revert_enabled] = (
        lambda: None
    )
    app.dependency_overrides[active_branches.require_repository_reset_enabled] = (
        lambda: None
    )
    app.dependency_overrides[active_repo_crud.require_repository_squash_enabled] = (
        lambda: None
    )

    requests = [
        (
            "/api/models/owner/demo-model/branch/main/revert",
            {"ref": "commit-ref"},
        ),
        (
            "/api/models/owner/demo-model/branch/main/reset",
            {"ref": "commit-ref", "force": True},
        ),
        ("/api/repos/squash", {"repo": "owner/demo-model", "type": "model"}),
    ]

    try:
        responses = [
            await client.post(path, json=payload) for path, payload in requests
        ]
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in responses] == [401, 401, 401]
    assert mutation_calls == []


def test_repository_operations_stay_disabled_on_sqlite(monkeypatch):
    for backend in ("sqlite", "POSTGRES"):
        monkeypatch.setattr(operation_capabilities.cfg.app, "db_backend", backend)
        monkeypatch.setattr(
            operation_capabilities.cfg.app, "repository_revert_enabled", True
        )
        monkeypatch.setattr(
            operation_capabilities.cfg.app, "repository_reset_enabled", True
        )
        monkeypatch.setattr(
            operation_capabilities.cfg.app, "repository_squash_enabled", True
        )

        assert operation_capabilities.get_repository_operation_capabilities() == {
            "revert": False,
            "reset": False,
            "squash": False,
        }


async def test_whoami_v2_requires_auth_and_returns_orgs(app, owner_client):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as anonymous_client:
        anonymous_response = await anonymous_client.get("/api/whoami-v2")
        assert anonymous_response.status_code == 401

    authenticated_response = await owner_client.get("/api/whoami-v2")
    assert authenticated_response.status_code == 200
    payload = authenticated_response.json()
    assert payload["name"] == "owner"
    assert any(org["name"] == "acme-labs" for org in payload["orgs"])


async def test_whoami_v2_bearer_and_cookie_agree(app, owner_client, hf_api_token):
    """A bearer-token caller and a cookie-session caller for the same user
    must receive identical ``/api/whoami-v2`` payloads (at minimum:
    ``name`` and the set of ``orgs``). Gradio deploys rely on the bearer
    path; the web UI relies on the cookie path — both must stay in sync."""
    cookie_response = await owner_client.get("/api/whoami-v2")
    cookie_response.raise_for_status()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as bearer_client:
        bearer_response = await bearer_client.get(
            "/api/whoami-v2",
            headers={"Authorization": f"Bearer {hf_api_token}"},
        )
    bearer_response.raise_for_status()

    cookie_payload = cookie_response.json()
    bearer_payload = bearer_response.json()
    assert cookie_payload["name"] == bearer_payload["name"] == "owner"
    assert (
        {org["name"] for org in cookie_payload["orgs"]}
        == {org["name"] for org in bearer_payload["orgs"]}
    )


def _with_lakefs(monkeypatch, version):
    """Make this process believe it talks to LakeFS ``version``."""
    import importlib

    compat = importlib.import_module("kohakuhub.lakefs_compat")
    monkeypatch.setattr(compat, "_version", compat.parse(version))
    return importlib.import_module("kohakuhub.api.operation_capabilities")


def test_reset_is_off_on_a_lakefs_too_old_for_it(monkeypatch):
    capabilities = _with_lakefs(monkeypatch, "1.47.0")
    monkeypatch.setattr(capabilities.cfg.app, "db_backend", "postgres")
    for field in ("revert", "reset", "squash"):
        monkeypatch.setattr(capabilities.cfg.app, f"repository_{field}_enabled", True)

    assert capabilities.get_repository_operation_capabilities() == {
        "revert": True,
        "reset": False,
        "squash": True,
    }
    with pytest.raises(capabilities.HTTPException) as refused:
        capabilities.ensure_repository_operation_enabled("reset")
    assert refused.value.status_code == 503
    assert "LakeFS 1.47.0 is older than 1.48.1" in refused.value.detail["message"]
    assert "docs/deployment/lakefs.md" in refused.value.detail["error"]

    # Switched off by configuration: that is the reason given
    monkeypatch.setattr(capabilities.cfg.app, "repository_reset_enabled", False)
    with pytest.raises(capabilities.HTTPException) as refused:
        capabilities.ensure_repository_operation_enabled("reset")
    assert refused.value.detail["message"] == "Repository Reset is temporarily disabled"
    monkeypatch.setattr(capabilities.cfg.app, "repository_reset_enabled", True)

    _with_lakefs(monkeypatch, "1.48.1")
    assert capabilities.get_repository_operation_capabilities()["reset"] is True


async def test_the_reset_endpoint_says_why_on_an_old_lakefs(client, monkeypatch):
    _with_lakefs(monkeypatch, "1.40.0")

    site = (await client.get("/api/site-config")).json()
    response = await client.post(
        "/api/models/owner/demo-model/branch/main/reset", json={"ref": "main"}
    )

    assert site["capabilities"]["repository_operations"]["reset"] is False
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "operation_disabled"
    assert "Reset would leave a merge commit" in response.json()["detail"]["message"]


async def test_the_reset_gate_learns_the_lakefs_version_first(client, monkeypatch):
    import importlib

    compat = importlib.import_module("kohakuhub.lakefs_compat")
    monkeypatch.setattr(compat, "_version", None)

    response = await client.post(
        "/api/models/owner/demo-model/branch/main/reset", json={"ref": "main"}
    )

    assert compat.known().status in ("supported", "untested")  # the real LakeFS
    assert response.status_code != 503
