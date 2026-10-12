"""Authorization hardening regressions (organization roles, quota totals, invitation delete, unlike, validate-yaml).

Seed: ``owner`` is the only super-admin of ``acme-labs``, ``member`` is an admin,
``visitor`` is a visitor, ``outsider`` belongs to nothing. ``acme-labs/private-dataset``
is private. Every test mutates or reads shared state, so each one restores the baseline.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.backend_per_test

ORG = "acme-labs"
YAML_LIMIT = 1024 * 1024


async def _roles(client):
    response = await client.get(f"/org/{ORG}/members")
    assert response.status_code == 200
    return {m["user"]: m["role"] for m in response.json()["members"]}


# ---------- 1. organization roles ----------


async def test_admin_cannot_demote_or_remove_a_super_admin(member_client, owner_client):
    demote = await member_client.put(
        f"/org/{ORG}/members/owner", json={"role": "member"}
    )
    remove = await member_client.delete(f"/org/{ORG}/members/owner")

    assert demote.status_code == 403
    assert remove.status_code == 403
    assert (await _roles(owner_client))["owner"] == "super-admin"


async def test_admin_cannot_promote_to_super_admin(member_client, owner_client):
    promote_self = await member_client.put(
        f"/org/{ORG}/members/member", json={"role": "super-admin"}
    )
    promote_other = await member_client.put(
        f"/org/{ORG}/members/visitor", json={"role": "super-admin"}
    )
    add_super = await member_client.post(
        f"/org/{ORG}/members", json={"username": "outsider", "role": "super-admin"}
    )

    assert [promote_self.status_code, promote_other.status_code, add_super.status_code] == [403, 403, 403]
    roles = await _roles(owner_client)
    assert roles == {"owner": "super-admin", "member": "admin", "visitor": "visitor"}


async def test_unknown_role_strings_are_rejected(member_client, owner_client):
    arbitrary = await member_client.put(
        f"/org/{ORG}/members/member", json={"role": "root-of-everything"}
    )
    by_super_admin = await owner_client.put(
        f"/org/{ORG}/members/visitor", json={"role": "root-of-everything"}
    )
    on_add = await owner_client.post(
        f"/org/{ORG}/members", json={"username": "outsider", "role": "root-of-everything"}
    )

    assert [arbitrary.status_code, by_super_admin.status_code, on_add.status_code] == [400, 400, 400]
    assert "Invalid role" in arbitrary.json()["detail"]
    assert await _roles(owner_client) == {
        "owner": "super-admin", "member": "admin", "visitor": "visitor"
    }


async def test_last_super_admin_cannot_be_demoted_or_removed(owner_client):
    demote = await owner_client.put(f"/org/{ORG}/members/owner", json={"role": "admin"})
    remove = await owner_client.delete(f"/org/{ORG}/members/owner")

    assert demote.status_code == 409
    assert remove.status_code == 409
    assert "last super-admin" in demote.json()["detail"]
    assert (await _roles(owner_client))["owner"] == "super-admin"


async def test_super_admin_legitimate_paths_still_work(owner_client, member_client):
    # promote a second super-admin, then the first may step down and be removed
    assert (await owner_client.put(
        f"/org/{ORG}/members/member", json={"role": "super-admin"}
    )).status_code == 200
    assert (await owner_client.post(
        f"/org/{ORG}/members", json={"username": "outsider", "role": "super-admin"}
    )).status_code == 200
    assert (await owner_client.put(
        f"/org/{ORG}/members/owner", json={"role": "admin"}
    )).status_code == 200
    assert (await member_client.delete(f"/org/{ORG}/members/owner")).status_code == 200
    assert await _roles(member_client) == {
        "member": "super-admin", "outsider": "super-admin", "visitor": "visitor"
    }


async def test_admin_still_manages_members_and_admins(member_client, owner_client):
    assert (await owner_client.post(
        f"/org/{ORG}/members", json={"username": "outsider", "role": "admin"}
    )).status_code == 200
    assert (await member_client.put(
        f"/org/{ORG}/members/outsider", json={"role": "member"}
    )).status_code == 200
    assert (await member_client.put(
        f"/org/{ORG}/members/visitor", json={"role": "admin"}
    )).status_code == 200
    assert (await member_client.delete(f"/org/{ORG}/members/visitor")).status_code == 200
    assert await _roles(owner_client) == {
        "owner": "super-admin", "member": "admin", "outsider": "member"
    }


# ---------- 2. quota totals ----------


async def test_quota_total_is_limited_to_the_namespace_members(
    owner_client, member_client, visitor_client, outsider_client, app
):
    assert (await outsider_client.get("/api/quota/owner")).status_code == 403
    assert (await outsider_client.get(f"/api/quota/{ORG}")).status_code == 403
    assert (await member_client.get("/api/quota/owner")).status_code == 403
    assert (await outsider_client.get("/api/quota/no-such-namespace")).status_code == 403

    assert (await owner_client.get("/api/quota/owner")).status_code == 200
    # org members of any role read the organization total, as /public already lets them
    for client in (owner_client, member_client, visitor_client):
        response = await client.get(f"/api/quota/{ORG}")
        assert response.status_code == 200
        assert response.json()["namespace"] == ORG

    # the public view is unchanged for a signed-in stranger
    public = await outsider_client.get("/api/quota/owner/public")
    assert public.status_code == 200
    assert public.json()["can_see_private"] is False


# ---------- 3. invitation delete ----------


def _register_invitation(token, creator):
    from kohakuhub.db import User
    from kohakuhub.db_operations import create_invitation

    return create_invitation(
        token=token,
        action="register_account",
        parameters=json.dumps({"email": "x@example.com"}),
        created_by=User.get(username=creator) if creator else None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )


def _exists(token):
    from kohakuhub.db_operations import get_invitation

    return get_invitation(token) is not None


async def test_only_the_creator_deletes_a_register_invitation(
    owner_client, member_client, outsider_client
):
    _register_invitation("reg-token-1", "owner")
    for stranger in (outsider_client, member_client):
        assert (await stranger.delete("/api/invitations/reg-token-1")).status_code == 403
    assert _exists("reg-token-1")

    assert (await owner_client.delete("/api/invitations/reg-token-1")).status_code == 200
    assert not _exists("reg-token-1")


async def test_system_made_invitation_is_not_deletable_here(outsider_client, owner_client):
    _register_invitation("reg-token-2", None)
    assert (await outsider_client.delete("/api/invitations/reg-token-2")).status_code == 403
    assert (await owner_client.delete("/api/invitations/reg-token-2")).status_code == 403
    assert _exists("reg-token-2")


async def test_org_invitation_delete_keeps_the_admin_rule(
    owner_client, member_client, visitor_client, outsider_client
):
    token = (await owner_client.post(
        f"/api/invitations/org/{ORG}/create", json={"role": "member"}
    )).json()["token"]

    assert (await visitor_client.delete(f"/api/invitations/{token}")).status_code == 403
    assert (await outsider_client.delete(f"/api/invitations/{token}")).status_code == 403
    assert (await member_client.delete(f"/api/invitations/{token}")).status_code == 200
    assert (await owner_client.delete(f"/api/invitations/{token}")).status_code == 404


# ---------- 4. like / unlike of an unreadable repository ----------


def _same(a, b):
    drop = {"x-request-id", "content-length", "date"}
    strip = lambda r: {k: v for k, v in r.headers.items() if k not in drop}
    return (a.status_code, a.content, strip(a)) == (b.status_code, b.content, strip(b))


async def test_unreadable_private_repo_looks_like_a_missing_one(outsider_client):
    private = "datasets/acme-labs/private-dataset"
    missing = "datasets/acme-labs/no-such-dataset"
    for verb in ("delete", "post", "get"):
        hidden = await getattr(outsider_client, verb)(f"/api/{private}/like")
        absent = await getattr(outsider_client, verb)(f"/api/{missing}/like")
        assert absent.status_code == 404
        assert hidden.status_code == absent.status_code, verb
        # the repository id is the only thing that differs in the message
        assert hidden.content == absent.content.replace(b"no-such-dataset", b"private-dataset"), verb
        assert hidden.headers.get("x-error-code") == absent.headers.get("x-error-code")


async def test_unlike_still_works_for_a_reader_of_a_private_repo(member_client):
    path = "/api/datasets/acme-labs/private-dataset/like"
    assert (await member_client.post(path)).status_code == 200
    unliked = await member_client.delete(path)
    assert unliked.status_code == 200
    assert unliked.json()["likes_count"] == 0
    # a reader who never liked still gets the honest error
    again = await member_client.delete(path)
    assert again.status_code == 400


# ---------- 5. validate-yaml ----------


async def _anonymous(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_validate_yaml_is_bounded_but_stays_anonymous(app):
    async with await _anonymous(app) as anonymous:
        over = await anonymous.post("/api/validate-yaml", json={"content": "a" * (YAML_LIMIT + 1)})
        at_limit = await anonymous.post("/api/validate-yaml", json={"content": "a" * YAML_LIMIT})
        multibyte = await anonymous.post(
            "/api/validate-yaml", json={"content": "é" * (YAML_LIMIT // 2 + 1)}
        )
        ok = await anonymous.post("/api/validate-yaml", json={"content": "model:\n  name: demo\n"})
        bad = await anonymous.post("/api/validate-yaml", json={"content": "model: [broken"})

    assert over.status_code == 413
    assert "too large" in over.json()["detail"]
    assert multibyte.status_code == 413  # the limit counts bytes, not characters
    assert at_limit.status_code == 200 and at_limit.json() == {"valid": True}
    assert ok.json() == {"valid": True}
    assert bad.status_code == 200 and bad.json() == {"valid": False}
