"""Repository invitation lifecycle, real SQL and concurrent redemption."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from peewee import OperationalError, ProgrammingError, SqliteDatabase

from kohakuhub import repository_invites as service
from kohakuhub.auth.permissions import RepoReadDeniedError, check_repo_read_permission
from kohakuhub.db import (
    RepositoryAccessEvent,
    RepositoryAccessGrant,
    RepositoryAccessRequest,
    RepositoryGuestSession,
    RepositoryInvite,
    RepositoryInviteRedemption,
    User,
    UserOrganization,
    Repository,
    utcnow,
)
from test.kohakuhub.support.factories import make_org, make_repo, make_user
from test.kohakuhub.support.db import table_missing


@pytest.fixture
def context(db_dual):
    owner, reader, outsider = [make_user(name) for name in ("owner", "reader", "outsider")]
    repo = make_repo(owner, "invite", private=True, invite_mode=True)
    other = make_repo(owner, "other", private=True)
    return owner, reader, outsider, repo, other


def issue(context, **kwargs):
    owner, _, _, repo, _ = context
    return service.issue_invite(repo, owner, expires_at=utcnow() + timedelta(days=7), **kwargs)


def test_issue_stores_only_a_hash_and_single_use_limits(context):
    row, secret = issue(context)
    assert secret.startswith("repo_") and len(secret) > 40
    assert row.token_hash != secret and len(row.token_hash) == 64
    assert (row.max_redemptions, row.max_guest_sessions, row.guest_ttl_seconds) == (1, 1, 86400)
    assert row.grant_expires_at is None
    assert secret not in str(list(RepositoryAccessEvent.select().dicts()))
    assert secret not in str(list(RepositoryInvite.select().dicts()))
    assert service.find_invite(secret).id == row.id
    for invalid in (None, "invalid", "repo_" + "x" * 300, "repo_unknown"):
        assert service.find_invite(invalid) is None


def test_private_repository_supports_issue_and_only_managers_can_issue_or_revoke(context):
    owner, reader, outsider, repo, private = context
    row, _ = service.issue_invite(private, owner, expires_at=utcnow() + timedelta(days=1))
    for actor in (reader, outsider, None):
        with pytest.raises(HTTPException):
            service.issue_invite(repo, actor, expires_at=utcnow() + timedelta(days=1))
        with pytest.raises(HTTPException):
            service.revoke_invite(private, row.id, actor)
    assert RepositoryInvite.select().count() == 1
    with pytest.raises(service.RepositoryInviteError):
        service.revoke_invite(repo, row.id, owner)


@pytest.mark.parametrize("overrides", [
    {"max_redemptions": 0}, {"max_guest_sessions": -1}, {"guest_ttl_seconds": 0},
    {"guest_ttl_seconds": 604801}, {"label": "x" * 101},
    {"expires_at": None}, {"expires_at": utcnow() - timedelta(seconds=1)},
])
def test_issue_rejects_invalid_limits_without_writes(context, overrides):
    owner, _, _, repo, _ = context
    params = {"expires_at": utcnow() + timedelta(days=1), **overrides}
    with pytest.raises(service.RepositoryInviteError):
        service.issue_invite(repo, owner, **params)
    assert RepositoryInvite.select().count() == 0
    assert RepositoryAccessEvent.select().count() == 0


def test_redemption_grants_exactly_one_repo_and_repeating_is_idempotent(context):
    _, reader, _, repo, sibling = context
    row, secret = issue(context)
    grant = service.redeem_account(repo, secret, reader)
    repeated = service.redeem_account(repo, secret, reader)
    assert grant.id == repeated.id and grant.source == "invite"
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    assert RepositoryInviteRedemption.select().count() == 1
    assert UserOrganization.select().count() == 0
    assert check_repo_read_permission(repo, reader)
    with pytest.raises(RepoReadDeniedError):
        check_repo_read_permission(sibling, reader)
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_account(sibling, secret, reader)


def test_revoked_grants_are_never_reactivated_by_repeated_exchange(context):
    owner, reader, _, repo, _ = context
    row, secret = issue(context)
    grant = service.redeem_account(repo, secret, reader)
    service.revoke_grant(repo, grant.id, owner)
    events = RepositoryAccessEvent.select().count()
    service.revoke_grant(repo, grant.id, owner)
    assert RepositoryAccessEvent.select().count() == events
    assert service.redeem_account(repo, secret, reader).revoked_at is not None
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    with pytest.raises(RepoReadDeniedError):
        check_repo_read_permission(repo, reader)
    renewed = service.grant_account(repo, owner, reader)
    assert renewed.id == grant.id and renewed.revoked_at is None


def test_expiration_stops_new_exchange_but_does_not_revoke_existing_account_grant(context, monkeypatch):
    _, reader, outsider, repo, _ = context
    row, secret = issue(context)
    grant = service.redeem_account(repo, secret, reader)
    monkeypatch.setattr(service, "utcnow", lambda: row.expires_at + timedelta(seconds=1))
    assert service.redeem_account(repo, secret, reader).id == grant.id
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_account(repo, secret, outsider)
    assert check_repo_read_permission(repo, reader)


def test_redemption_joins_the_registration_transaction(context, db_dual):
    _, _, _, repo, _ = context
    row, secret = issue(context)
    with pytest.raises(RuntimeError), db_dual.atomic():
        user = make_user("new-registration")
        service.redeem_account(repo, secret, user)
        raise RuntimeError("registration failed after granting")
    assert not User.select().where(User.username == "new-registration").exists()
    assert RepositoryAccessGrant.select().count() == 0
    assert RepositoryInviteRedemption.select().count() == 0
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 0


@pytest.mark.parametrize("same_user", [False, True])
def test_concurrent_exchange_is_bounded_and_duplicate_account_exchange_is_idempotent(context, db_dual, same_user):
    _, reader, outsider, repo, _ = context
    row, secret = issue(context)

    def exchange(user):
        try:
            return service.redeem_account(repo, secret, user).id
        except service.RepositoryInviteError:
            return None
        finally:
            db_dual.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(exchange, [reader, reader if same_user else outsider]))
    assert sum(value is not None for value in result) == (2 if same_user else 1)
    if same_user:
        assert result[0] == result[1]
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    assert RepositoryAccessGrant.select().count() == 1
    assert RepositoryInviteRedemption.select().count() == 1


def test_anonymous_request_requires_email_and_uses_hashed_receipt_without_email_lookup(context):
    _, _, _, repo, sibling = context
    for email in (None, "", "invalid"):
        with pytest.raises(service.RepositoryInviteError):
            service.submit_request(repo, email=email)
    request, receipt = service.submit_request(repo, email="Applicant@Example.com", message="Research project")
    assert receipt and request.receipt_hash != receipt
    assert service.get_request_status(repo, receipt=receipt).id == request.id
    assert service.get_request_status(repo, receipt="wrong") is None
    assert service.get_request_status(sibling, receipt=receipt) is None
    assert service.get_request_status(repo) is None
    assert RepositoryAccessGrant.select().count() == 0


def test_receipt_remains_usable_after_login_without_overriding_account_owned_request(context):
    _, reader, outsider, repo, sibling = context
    anonymous, receipt = service.submit_request(repo, email="reader@example.com")
    assert service.get_request_status(repo, user=reader, receipt=receipt).id == anonymous.id
    assert service.get_request_status(repo, user=reader, receipt="invalid") is None
    assert service.get_request_status(sibling, user=reader, receipt=receipt) is None
    owned, _ = service.submit_request(repo, reader)
    assert service.get_request_status(repo, user=reader, receipt=receipt).id == owned.id
    # Receipts authorize anonymous applications only, never another account's application.
    owned.receipt_hash = anonymous.receipt_hash
    anonymous.receipt_hash = None
    anonymous.save()
    owned.save()
    assert service.get_request_status(repo, user=outsider, receipt=receipt) is None
    assert service.get_request_status(repo, receipt=receipt) is None


def test_logged_in_approval_grants_directly_without_code_and_repeat_does_not_restore_revocation(context):
    owner, reader, outsider, repo, _ = context
    request, receipt = service.submit_request(repo, reader, message="Research project")
    assert receipt is None and service.submit_request(repo, reader)[0].id == request.id
    assert service.get_request_status(repo, user=reader).id == request.id
    assert service.get_request_status(repo, user=outsider) is None
    with pytest.raises(HTTPException):
        service.review_request(repo, request.id, outsider, approve=True)
    reviewed = service.review_request(repo, request.id, owner, approve=True)
    assert reviewed.status == "approved" and reviewed.reviewed_by_id == owner.id
    assert RepositoryInvite.select().count() == 0
    grant = RepositoryAccessGrant.get(RepositoryAccessGrant.user == reader)
    assert grant.source == "request" and check_repo_read_permission(repo, reader)
    service.revoke_grant(repo, grant.id, owner)
    assert service.review_request(repo, request.id, owner, approve=True).status == "approved"
    assert RepositoryAccessGrant.get_by_id(grant.id).revoked_at is not None


def test_anonymous_approval_does_not_issue_code_until_manager_explicitly_issues(context):
    owner, _, _, repo, sibling = context
    request, _ = service.submit_request(repo, email="reader@example.com")
    with pytest.raises(service.RepositoryInviteError):
        issue(context, access_request=request)
    reviewed = service.review_request(repo, request.id, owner, approve=True)
    assert reviewed.status == "approved"
    assert RepositoryInvite.select().count() == RepositoryAccessGrant.select().count() == 0
    row, _ = issue(context, access_request=reviewed)
    assert row.access_request_id == request.id
    with pytest.raises(service.RepositoryInviteError):
        service.issue_invite(sibling, owner, expires_at=utcnow() + timedelta(days=1), access_request=reviewed)


def test_rejection_and_review_cross_repo_protection(context):
    owner, reader, _, repo, sibling = context
    request, _ = service.submit_request(repo, reader)
    with pytest.raises(service.RepositoryInviteError):
        service.review_request(sibling, request.id, owner, approve=True)
    assert service.review_request(repo, request.id, owner, approve=False).status == "rejected"
    with pytest.raises(service.RepositoryInviteError):
        service.review_request(repo, request.id, owner, approve=True)
    assert RepositoryAccessGrant.select().count() == 0


def test_guests_are_separate_bounded_sessions_and_invite_revocation_preserves_account_grants(context, monkeypatch):
    owner, reader, _, repo, sibling = context
    row, secret = issue(context)
    session, session_secret = service.redeem_guest(repo, secret)
    repeated_session, repeated_secret = service.redeem_guest(repo, secret)
    grant = service.redeem_account(repo, secret, reader)
    assert session.token_hash != session_secret and session.invite_id == row.id
    assert service.get_guest_session(repo, session_secret).id == session.id
    assert service.get_guest_session(sibling, session_secret) is None
    assert service.get_guest_session(repo, "bad") is None
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_guest(repo, secret)
    assert RepositoryInvite.get_by_id(row.id).guest_session_count == 2
    service.revoke_invite(repo, row.id, owner)
    assert service.get_guest_session(repo, repeated_secret) is None
    assert RepositoryGuestSession.get_by_id(repeated_session.id).revoked_at is not None
    assert service.get_guest_session(repo, session_secret) is None
    assert RepositoryGuestSession.get_by_id(session.id).revoked_at is not None
    assert RepositoryAccessGrant.get_by_id(grant.id).revoked_at is None
    assert service.redeem_account(repo, secret, reader).id == grant.id


def test_guest_expiry_is_independent_of_exchange_expiry(context, monkeypatch):
    owner, _, _, repo, _ = context
    now = utcnow()
    row, secret = service.issue_invite(repo, owner, expires_at=now + timedelta(seconds=5))
    session, session_secret = service.redeem_guest(repo, secret)
    monkeypatch.setattr(service, "utcnow", lambda: now + timedelta(minutes=1))
    assert service.get_guest_session(repo, session_secret).id == session.id
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_guest(repo, secret)
    monkeypatch.setattr(service, "utcnow", lambda: session.expires_at)
    assert service.get_guest_session(repo, session_secret) is None


def test_guest_session_revocation_and_private_repo_support(context):
    owner, _, outsider, repo, private = context
    _, secret = issue(context)
    session, session_secret = service.redeem_guest(repo, secret)
    with pytest.raises(HTTPException):
        service.revoke_guest_session(repo, session.id, outsider)
    service.revoke_guest_session(repo, session.id, owner)
    assert service.get_guest_session(repo, session_secret) is None
    row, private_secret = service.issue_invite(private, owner, expires_at=utcnow() + timedelta(days=1))
    private_session, private_guest = service.redeem_guest(private, private_secret)
    assert service.get_guest_session(private, private_guest).id == private_session.id
    assert RepositoryInvite.get_by_id(row.id).guest_session_count == 1
    Repository.update(invite_mode=False).where(Repository.id == repo.id).execute()
    assert service.get_guest_session(private, private_guest).id == private_session.id


def test_disabling_invitation_mode_keeps_guest_until_explicit_revocation(context):
    _, _, _, repo, _ = context
    _, secret = issue(context)
    session, guest_secret = service.redeem_guest(repo, secret)
    Repository.update(invite_mode=False).where(Repository.id == repo.id).execute()
    assert service.get_guest_session(repo, guest_secret).id == session.id
    Repository.update(private=False).where(Repository.id == repo.id).execute()
    assert service.get_guest_session(repo, guest_secret) is None


def test_org_admin_management_and_regular_members_cannot_manage(context):
    owner, reader, outsider, _, _ = context
    org = make_org("org", admin=owner)
    repo = make_repo(org, "private", private=True)
    UserOrganization.create(user=reader, organization=org, role="member")
    with pytest.raises(HTTPException):
        service.grant_account(repo, reader, outsider)
    grant = service.grant_account(repo, owner, outsider)
    assert check_repo_read_permission(repo, outsider)
    service.revoke_grant(repo, grant.id, owner)
    with pytest.raises(RepoReadDeniedError):
        check_repo_read_permission(repo, outsider)


def test_inactive_and_organization_accounts_cannot_exchange_or_receive_grants(context):
    owner, _, _, repo, _ = context
    _, secret = issue(context)
    for user in (None, make_user("disabled", is_active=False), make_org("organization")):
        with pytest.raises(service.RepositoryInviteError):
            service.redeem_account(repo, secret, user)
        with pytest.raises(service.RepositoryInviteError):
            service.grant_account(repo, owner, user)


def test_dates_are_normalized_and_malformed_expirations_rejected(context):
    owner, _, _, repo, _ = context
    aware = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)
    row, _ = service.issue_invite(repo, owner, expires_at=aware)
    assert row.expires_at == aware.astimezone(timezone.utc).replace(tzinfo=None)
    with pytest.raises(service.RepositoryInviteError):
        service.issue_invite(repo, owner, expires_at="tomorrow")
    with pytest.raises(service.RepositoryInviteError):
        service.issue_invite(repo, owner, expires_at=utcnow() + timedelta(days=400))
    with pytest.raises(service.RepositoryInviteError):
        issue(context, grant_expires_at=utcnow() - timedelta(seconds=1))


def test_account_expiry_is_independent_of_code_and_new_codes_do_not_restore_revoked_grants(context, monkeypatch):
    owner, reader, outsider, repo, _ = context
    end = utcnow() + timedelta(hours=1)
    row, secret = issue(context, grant_expires_at=end)
    grant = service.redeem_account(repo, secret, reader)
    assert grant.expires_at == end
    monkeypatch.setattr(service, "utcnow", lambda: end + timedelta(seconds=1))
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_account(repo, secret, outsider)
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    assert service.redeem_account(repo, secret, reader).expires_at == end
    service.revoke_grant(repo, grant.id, owner)
    another, new_secret = issue(context)
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_account(repo, new_secret, reader)
    assert RepositoryInvite.get_by_id(another.id).redemption_count == 0


def test_deleted_grants_are_not_recreated_by_repeating_exchange(context):
    _, reader, _, repo, _ = context
    _, secret = issue(context)
    grant = service.redeem_account(repo, secret, reader)
    RepositoryAccessGrant.delete().where(RepositoryAccessGrant.id == grant.id).execute()
    with pytest.raises(service.RepositoryInviteError) as error:
        service.redeem_account(repo, secret, reader)
    assert error.value.code == "grant_inactive"
    assert RepositoryAccessGrant.select().count() == 0


def test_invalid_codes_and_repository_states_are_rejected_without_consuming(context):
    owner, reader, _, repo, _ = context
    row, secret = issue(context)
    for invalid in (None, "invalid", "repo_missing", "repo_" + "x" * 300):
        with pytest.raises(service.RepositoryInviteError):
            service.redeem_account(repo, invalid, reader)
    public = make_repo(owner, "public")
    with pytest.raises(service.RepositoryInviteError):
        service.issue_invite(public, owner, expires_at=utcnow() + timedelta(days=1))
    Repository.update(private=False, invite_mode=False).where(Repository.id == repo.id).execute()
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_account(repo, secret, reader)
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_guest(repo, secret)
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 0


def test_missing_and_cross_repository_manager_actions_are_rejected(context):
    owner, reader, _, repo, sibling = context
    grant = service.grant_account(repo, owner, reader)
    _, secret = issue(context)
    session, _ = service.redeem_guest(repo, secret)
    request, _ = service.submit_request(repo, reader)
    for operation, identifier in (
        (service.revoke_grant, grant.id), (service.revoke_guest_session, session.id),
    ):
        for target in (sibling,):
            with pytest.raises(service.RepositoryInviteError):
                operation(target, identifier, owner)
    with pytest.raises(service.RepositoryInviteError):
        service.review_request(repo, request.id + 1000, owner, approve=True)
    assert RepositoryAccessGrant.get_by_id(grant.id).revoked_at is None


def test_invalid_request_and_grant_inputs_are_rejected(context):
    owner, reader, _, repo, private = context
    for message in (None, "x" * 4001):
        with pytest.raises(service.RepositoryInviteError):
            service.submit_request(repo, reader, message=message)
    with pytest.raises(service.RepositoryInviteError):
        service.submit_request(private, reader)
    with pytest.raises(service.RepositoryInviteError):
        service.grant_account(repo, owner, reader, expires_at=utcnow() - timedelta(seconds=1))
    with pytest.raises(service.RepositoryInviteError):
        service.grant_account(repo, owner, reader, source="organization")
    assert RepositoryAccessRequest.select().count() == 0


@pytest.mark.parametrize("guest", [False, True])
def test_audit_write_failure_rolls_back_grants_sessions_and_consumed_counts(context, db_dual, guest):
    _, reader, _, repo, _ = context
    row, secret = issue(context)
    # A real database failure, rather than a fake ORM, proves all writes share the transaction.
    # A missing table is OperationalError on SQLite and ProgrammingError on PostgreSQL.
    expected_error = OperationalError if isinstance(db_dual, SqliteDatabase) else ProgrammingError
    with table_missing(db_dual, RepositoryAccessEvent), pytest.raises(expected_error, match="repository_access_event"):
        if guest:
            service.redeem_guest(repo, secret)
        else:
            service.redeem_account(repo, secret, reader)
    assert RepositoryAccessGrant.select().count() == RepositoryGuestSession.select().count() == 0
    assert RepositoryInviteRedemption.select().count() == 0
    row = RepositoryInvite.get_by_id(row.id)
    assert row.redemption_count == row.guest_session_count == 0


def test_concurrent_guest_sessions_do_not_consume_account_binding(context, db_dual):
    _, _, _, repo, _ = context
    row, secret = issue(context)

    def exchange(_):
        try:
            return service.redeem_guest(repo, secret)[0].id
        except service.RepositoryInviteError:
            return None
        finally:
            db_dual.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(exchange, [0, 1]))
    assert len(set(results)) == 2 and None not in results
    row = RepositoryInvite.get_by_id(row.id)
    assert row.redemption_count == 0 and row.guest_session_count == 2
    assert RepositoryGuestSession.select().count() == 2


def test_concurrent_guest_issue_and_invitation_revocation_leave_no_active_guest(context, db_dual):
    owner, _, _, repo, _ = context
    row, secret = issue(context)

    def operation(revoke):
        try:
            if revoke:
                service.revoke_invite(repo, row.id, owner)
                return None
            return service.redeem_guest(repo, secret)[1]
        except service.RepositoryInviteError:
            return None
        finally:
            db_dual.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operation, [False, True]))
    for session_secret in filter(None, results):
        assert service.get_guest_session(repo, session_secret) is None
    assert RepositoryInvite.get_by_id(row.id).revoked_at is not None
    assert not RepositoryGuestSession.select().where(RepositoryGuestSession.revoked_at.is_null()).exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_reusable_guest_code_can_later_bind_exactly_one_account(context, legacy):
    _, reader, outsider, repo, _ = context
    row, secret = issue(context)
    if legacy:
        RepositoryInvite.update(max_redemptions=5, max_guest_sessions=1).where(RepositoryInvite.id == row.id).execute()
    guests = [service.redeem_guest(repo, secret) for _ in range(3)]
    actual = RepositoryInvite.get_by_id(row.id)
    assert actual.redemption_count == 0 and actual.guest_session_count == 3
    grant = service.redeem_account(repo, secret, reader)
    assert check_repo_read_permission(repo, reader)
    assert service.redeem_account(repo, secret, reader).id == grant.id
    assert all(service.get_guest_session(repo, guest_secret).id == session.id for session, guest_secret in guests)
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_account(repo, secret, outsider)
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_guest(repo, secret)
    actual = RepositoryInvite.get_by_id(row.id)
    assert (actual.redemption_count, actual.guest_session_count) == (1, 3)
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_account_binding_serializes_with_guest_opening_and_blocks_later_openings(context, db_dual, legacy):
    from threading import Barrier
    _, reader, _, repo, _ = context
    row, secret = issue(context)
    if legacy:
        RepositoryInvite.update(max_redemptions=5, max_guest_sessions=1).where(RepositoryInvite.id == row.id).execute()
    ready = Barrier(2)
    def exchange(kind):
        try:
            ready.wait(timeout=10)
            if kind == "account":
                service.redeem_account(repo, secret, reader)
            else:
                service.redeem_guest(repo, secret)
            return kind
        except service.RepositoryInviteError:
            return None
        finally:
            db_dual.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(exchange, ["account", "guest"]))
    assert results[0] == "account"
    actual = RepositoryInvite.get_by_id(row.id)
    assert actual.redemption_count == 1
    assert actual.guest_session_count == int(results[1] == "guest")
    actions = list(RepositoryAccessEvent.select(RepositoryAccessEvent.action).where(RepositoryAccessEvent.action.in_(["invite_redeemed", "guest_session_issued"])).order_by(RepositoryAccessEvent.id).scalars())
    assert len(actions) == 1 + actual.guest_session_count
    assert actions[-1] == "invite_redeemed"
    with pytest.raises(service.RepositoryInviteError):
        service.redeem_guest(repo, secret)


@pytest.mark.parametrize("limits", [
    {"max_redemptions": 2}, {"max_guest_sessions": 2}, {"max_guest_sessions": 0},
])
def test_issue_rejects_obsolete_quota_configuration(context, limits):
    with pytest.raises(service.RepositoryInviteError):
        issue(context, **limits)
    assert RepositoryInvite.select().count() == 0
