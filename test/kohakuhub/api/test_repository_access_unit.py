"""Browser access preflight exposes only invitation instructions before approval."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from kohakuhub.api import repository_access as access
from kohakuhub.auth.dependencies import get_current_user, get_current_user_or_admin, get_optional_user
from kohakuhub.auth.permissions import RepoReadDeniedError
from kohakuhub.api.repo.utils.hf import hf_repo_not_found
from kohakuhub.db import RepositoryAccessGrant, RepositoryAccessRequest, RepositoryGuestSession, RepositoryInvite, RepositoryInviteRedemption
from test.kohakuhub.support.factories import make_repo, make_user


@pytest.fixture
def browser(db_fresh, monkeypatch):
    owner, reader = make_user(), make_user("reader")
    repo = make_repo(owner, "invited", private=True, invite_mode=True)
    lakefs = AsyncMock()
    lakefs.get_branch.return_value = {"commit_id": "head"}
    lakefs.get_object_prefix.return_value = b"# Please apply\n![private](secret.png)"
    monkeypatch.setattr(access, "get_lakefs_client", lambda: lakefs)
    state = {"user": None}
    app = FastAPI()
    app.include_router(access.router, prefix="/api")
    app.dependency_overrides[get_optional_user] = lambda: state["user"]
    def current_user():
        from fastapi import HTTPException
        if state["user"] is None:
            raise HTTPException(401, "Not authenticated")
        return state["user"]
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_current_user_or_admin] = lambda: (current_user(), False)
    @app.exception_handler(RepoReadDeniedError)
    async def denied(request, error):
        return hf_repo_not_found(error.repo_id, error.repo_type)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, owner=owner, reader=reader, repo=repo, state=state, lakefs=lakefs, base="/api/models/owner/invited")


def test_unapproved_invite_exposes_only_exact_public_instructions(browser):
    result = browser.client.get(browser.base + "/access")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {
        "access": "invite", "visibility": "invite", "can_manage": False,
        "invite_markdown": "# Please apply\n![private](secret.png)",
    }
    browser.lakefs.get_branch.assert_awaited_once()
    args = browser.lakefs.get_object_prefix.await_args.args
    assert args[1:] == ("head", "INVITE.MD", access.MAX_INVITE_BYTES + 1)
    browser.lakefs.list_objects.assert_not_awaited()
    browser.lakefs.get_object.assert_not_awaited()


@pytest.mark.parametrize("content", [b"x" * (65536 + 1), b"\xff\xfe", b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 123\n"], ids=["oversize", "non-utf8", "lfs-pointer"])
def test_unusable_instructions_fall_back_without_resolving_other_files(browser, content):
    browser.lakefs.get_object_prefix.return_value = content
    assert browser.client.get(browser.base + "/access").json()["invite_markdown"] is None
    browser.lakefs.get_object.assert_not_awaited()


def test_missing_invite_md_uses_default_page(browser):
    browser.lakefs.get_object_prefix.side_effect = httpx.HTTPStatusError(
        "missing", request=httpx.Request("GET", "https://storage.invalid"), response=httpx.Response(404)
    )
    assert browser.client.get(browser.base + "/access").json()["invite_markdown"] is None


def test_storage_outage_fails_closed_without_fallback(browser):
    browser.lakefs.get_object_prefix.side_effect = RuntimeError("storage secret internal URL")
    response = browser.client.get(browser.base + "/access")
    assert response.status_code == 503
    assert "storage secret" not in response.text


def test_standard_public_private_and_grantee_states(browser):
    assert browser.client.get("/api/models/owner/missing/access").json()["access"] == "standard"
    private = make_repo(browser.owner, "hidden", private=True)
    denied = browser.client.get("/api/models/owner/hidden/access")
    assert denied.status_code == 200
    assert denied.json() == browser.client.get("/api/models/owner/missing/access").json()
    browser.state["user"] = browser.reader
    assert browser.client.get("/api/models/owner/hidden/access").json() == denied.json()
    RepositoryAccessGrant.create(repository=private, user=browser.reader)
    assert browser.client.get("/api/models/owner/hidden/access").json()["access"] == "full"
    browser.state["user"] = browser.owner
    assert browser.client.get(browser.base + "/access").json()["can_manage"] is True
    browser.lakefs.get_object_prefix.assert_not_awaited()


def test_signed_in_application_requires_review_and_grants_without_a_code(browser):
    browser.state["user"] = browser.reader
    application = browser.client.post(browser.base + "/access/requests", json={"message": "research"})
    assert application.status_code == 200
    row = application.json()["request"]
    assert row["status"] == "pending" and application.json()["receipt"] is None
    assert browser.client.get(browser.base + "/access").json()["access"] == "invite"
    assert browser.client.get(browser.base + "/access/manage").status_code == 403
    assert browser.client.post(browser.base + "/access/invites", json={}).status_code == 403
    assert browser.client.post(browser.base + f"/access/requests/{row['id']}/review", json={"approve": True}).status_code == 403
    browser.state["user"] = browser.owner
    approved = browser.client.post(browser.base + f"/access/requests/{row['id']}/review", json={"approve": True})
    assert approved.status_code == 200 and approved.json()["request"]["status"] == "approved"
    assert RepositoryInvite.select().count() == 0
    browser.state["user"] = browser.reader
    assert browser.client.get(browser.base + "/access/request").json()["request"]["status"] == "approved"
    assert browser.client.get(browser.base + "/access").json()["access"] == "full"
    assert browser.client.get(browser.base + "/access").json()["can_manage"] is False


def test_anonymous_receipt_and_approved_issue_do_not_publish_secrets(browser):
    assert browser.client.post(browser.base + "/access/requests", json={}).status_code == 400
    result = browser.client.post(browser.base + "/access/requests", json={"email": "reader@example.com"}).json()
    receipt = result["receipt"]
    request_id = result["request"]["id"]
    assert receipt and browser.client.get(browser.base + "/access/request").json()["request"] is None
    assert browser.client.get(browser.base + "/access/request", params={"email": "reader@example.com"}).json()["request"] is None
    assert browser.client.get(browser.base + "/access/request", headers={"X-Repository-Request-Receipt": receipt}).json()["request"]["id"] == request_id
    browser.state["user"] = browser.owner
    assert browser.client.post(browser.base + "/access/invites", json={"request_id": request_id}).status_code == 400
    assert browser.client.post(browser.base + f"/access/requests/{request_id}/review", json={"approve": True}).status_code == 200
    issued = browser.client.post(browser.base + "/access/invites", json={"request_id": request_id})
    assert issued.status_code == 200
    code = issued.json()["token"]
    listed = browser.client.get(browser.base + "/access/manage")
    assert listed.status_code == 200
    for forbidden in (code, receipt, "token_hash", "receipt_hash"):
        assert forbidden not in listed.text
    assert listed.json()["invitations"][0]["request_id"] == request_id


def test_binding_guest_and_revocation_have_independent_lifetimes(browser):
    browser.state["user"] = browser.owner
    issued = browser.client.post(browser.base + "/access/invites", json={}).json()
    token, invitation_id = issued["token"], issued["invitation"]["id"]
    account_code = browser.client.post(browser.base + "/access/invites", json={}).json()["token"]
    browser.state["user"] = None
    browser.client.cookies.set("session_id", "existing-normal-login-cookie")
    guest = browser.client.post(browser.base + "/access/guest", json={"token": token})
    assert guest.status_code == 200
    assert "HttpOnly" in guest.headers["set-cookie"] and "SameSite=lax" in guest.headers["set-cookie"]
    assert browser.client.cookies.get("session_id") == "existing-normal-login-cookie"
    assert browser.client.get(browser.base + "/access").json()["access"] == "guest"
    assert browser.client.post(browser.base + "/access/bind", json={"token": token}).status_code == 401
    browser.state["user"] = browser.reader
    for _ in range(2):
        assert browser.client.post(browser.base + "/access/bind", json={"token": account_code}).status_code == 200
    assert RepositoryInvite.get_by_id(invitation_id).redemption_count == 0
    assert RepositoryInvite.get_by_id(invitation_id).guest_session_count == 1
    assert RepositoryInviteRedemption.select().count() == 1
    assert browser.client.get(browser.base + "/access").json()["access"] == "full"
    browser.state["user"] = browser.owner
    assert browser.client.post(browser.base + f"/access/invites/{invitation_id}/revoke").status_code == 200
    browser.state["user"] = None
    assert browser.client.get(browser.base + "/access").json()["access"] == "invite"
    browser.state["user"] = browser.reader
    assert browser.client.get(browser.base + "/access").json()["access"] == "full"
    browser.state["user"] = browser.owner
    grant_id = browser.client.get(browser.base + "/access/manage").json()["grants"][0]["id"]
    assert browser.client.post(browser.base + f"/access/grants/{grant_id}/revoke").status_code == 200
    browser.state["user"] = browser.reader
    assert browser.client.get(browser.base + "/access").json()["access"] == "invite"
    assert browser.client.post(browser.base + "/access/bind", json={"token": account_code}).status_code == 200
    assert browser.client.get(browser.base + "/access").json()["access"] == "invite"  # Repeat does not restore a revoked grant.


def test_management_pagination_keeps_older_applications_reachable(browser):
    browser.state["user"] = browser.owner
    for index in range(101):
        RepositoryAccessRequest.create(repository=browser.repo, email=f"applicant-{index}@example.com", message=str(index))
    first = browser.client.get(browser.base + "/access/manage").json()
    assert len(first["requests"]) == 100 and first["has_more"]["requests"] is True
    second = browser.client.get(browser.base + "/access/manage", params={"offset": 100}).json()
    assert [row["message"] for row in second["requests"]] == ["0"]
    assert second["has_more"]["requests"] is False


def test_new_unauthorized_routes_do_not_reveal_ordinary_private_existence(browser):
    make_repo(browser.owner, "hidden", private=True)
    for suffix, method, body in (("request", "GET", None), ("requests", "POST", {"email": "a@example.com"}), ("guest", "POST", {"token": "repo_unknown"})):
        results = []
        for name in ("hidden", "missing"):
            response = browser.client.request(method, f"/api/models/owner/{name}/access/{suffix}", json=body)
            results.append((response.status_code, response.content))
        assert results[0] == results[1]


def test_managers_can_grant_accounts_and_revoke_guests_independently(browser):
    browser.state["user"] = browser.owner
    assert browser.client.post(browser.base + "/access/grants", json={"username": "unknown"}).status_code == 404
    granted = browser.client.post(browser.base + "/access/grants", json={"username": "reader"})
    assert granted.status_code == 200 and granted.json()["grant"]["username"] == "reader"
    issued = browser.client.post(browser.base + "/access/invites", json={}).json()
    browser.state["user"] = None
    assert browser.client.post(browser.base + "/access/guest", json={"token": issued["token"]}).status_code == 200
    browser.state["user"] = browser.owner
    guest_id = browser.client.get(browser.base + "/access/manage").json()["guests"][0]["id"]
    assert browser.client.post(browser.base + f"/access/guests/{guest_id}/revoke").status_code == 200
    browser.state["user"] = None
    assert browser.client.get(browser.base + "/access").json()["access"] == "invite"
    assert browser.client.post(browser.base + "/access/guest", json={"token": issued["token"]}).status_code == 200
    assert browser.client.delete(browser.base + "/access/guest").json()["success"] is True
    assert browser.client.get(browser.base + "/access").json()["access"] == "invite"
    browser.state["user"] = browser.reader
    assert browser.client.get(browser.base + "/access").json()["access"] == "full"


def test_private_management_is_masked_and_unknown_application_cannot_be_issued(browser):
    make_repo(browser.owner, "hidden", private=True)
    browser.state["user"] = browser.reader
    results = [browser.client.get(f"/api/models/owner/{name}/access/manage") for name in ("hidden", "missing")]
    assert [(response.status_code, response.content) for response in results] == [(404, results[0].content)] * 2
    browser.state["user"] = browser.owner
    assert browser.client.post(browser.base + "/access/invites", json={"request_id": 99999}).status_code == 404
    assert RepositoryInvite.select().count() == 0


def test_empty_repository_instructions_use_default_without_any_object_read(browser):
    browser.lakefs.get_branch.return_value = {"commit_id": None}
    data = browser.client.get(browser.base + "/access").json()
    assert data["access"] == "invite" and not data["invite_markdown"]
    browser.lakefs.get_object_prefix.assert_not_awaited()


def test_guest_logout_without_guest_preserves_normal_cookie_and_masks_existence(browser):
    make_repo(browser.owner, "hidden", private=True)
    browser.client.cookies.set("session_id", "unchanged-normal-cookie")
    results = [browser.client.delete(f"/api/models/owner/{name}/access/guest") for name in ("hidden", "missing")]
    assert all(response.status_code == 200 and response.json() == {"success": True} for response in results)
    assert all("set-cookie" not in response.headers for response in results)
    assert browser.client.cookies.get("session_id") == "unchanged-normal-cookie"


@pytest.mark.parametrize("first_exchange", ["account", "guest"])
def test_one_account_binding_retires_code_without_revoking_existing_guest_sessions(browser, first_exchange):
    browser.state["user"] = browser.owner
    issued = browser.client.post(browser.base + "/access/invites", json={}).json()
    token, invite_id = issued["token"], issued["invitation"]["id"]
    guest_count = 0
    if first_exchange == "guest":
        browser.state["user"] = None
        for _ in range(2):
            assert browser.client.post(browser.base + "/access/guest", json={"token": token}).status_code == 200
        guest_count = 2
        assert browser.client.get(browser.base + "/access").json()["access"] == "guest"
    browser.state["user"] = browser.reader
    first = browser.client.post(browser.base + "/access/bind", json={"token": token})
    assert first.status_code == 200
    repeated = browser.client.post(browser.base + "/access/bind", json={"token": token})
    assert repeated.status_code == 200
    assert repeated.json()["grant"]["id"] == first.json()["grant"]["id"]
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1
    assert RepositoryGuestSession.select().count() == guest_count
    assert browser.client.get(browser.base + "/access").json()["access"] == "full"
    browser.state["user"] = None
    denied_guest = browser.client.post(browser.base + "/access/guest", json={"token": token})
    assert denied_guest.status_code == 400
    assert denied_guest.json()["detail"]["code"] == "invalid_invitation"
    assert token not in denied_guest.text
    assert browser.client.get(browser.base + "/access").json()["access"] == ("guest" if guest_count else "invite")
    browser.state["user"] = make_user("other-reader")
    denied_account = browser.client.post(browser.base + "/access/bind", json={"token": token})
    assert denied_account.status_code == 400
    assert denied_account.json()["detail"]["code"] == "invalid_invitation"
    row = RepositoryInvite.get_by_id(invite_id)
    assert (row.redemption_count, row.guest_session_count) == (1, guest_count)
    assert RepositoryGuestSession.select().where(RepositoryGuestSession.revoked_at.is_null(False)).count() == 0


@pytest.mark.parametrize("field", ["max_redemptions", "max_guest_sessions"])
@pytest.mark.parametrize("value", [0, 2, True, "1", 1.0])
def test_issuance_api_rejects_non_unit_or_non_integer_limits(browser, field, value):
    browser.state["user"] = browser.owner
    response = browser.client.post(browser.base + "/access/invites", json={field: value})
    assert response.status_code == 422
    assert RepositoryInvite.select().count() == 0


def test_issuance_api_accepts_legacy_explicit_single_use_limits(browser):
    browser.state["user"] = browser.owner
    response = browser.client.post(browser.base + "/access/invites", json={"max_redemptions": 1, "max_guest_sessions": 1})
    assert response.status_code == 200
    row = RepositoryInvite.get_by_id(response.json()["invitation"]["id"])
    assert (row.max_redemptions, row.max_guest_sessions) == (1, 1)
