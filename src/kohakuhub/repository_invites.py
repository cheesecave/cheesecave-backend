"""Repository applications, account grants, exchange codes and isolated guest identities.

Repository codes never satisfy site registration policy and are never authentication tokens.
The registration route may call ``redeem_account`` inside its own account-creation transaction.
Only dedicated controlled-preview endpoints may consume ``get_guest_session``.
"""

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from email_validator import EmailNotValidError, validate_email
from peewee import SqliteDatabase

from kohakuhub.auth.permissions import check_repo_manage_permission
from kohakuhub.db import (
    Repository,
    RepositoryAccessEvent,
    RepositoryAccessGrant,
    RepositoryAccessRequest,
    RepositoryGuestSession,
    RepositoryInvite,
    RepositoryInviteRedemption,
    User,
    utcnow,
)


class RepositoryInviteError(ValueError):
    """A domain failure that the HTTP boundary can map without exposing a secret."""

    def __init__(self, message: str, code: str = "invalid_invitation"):
        self.code = code
        self.message = message
        super().__init__(message)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@contextmanager
def _atomic():
    database = RepositoryInvite._meta.database
    # SQLite has no SELECT FOR UPDATE: acquire its writer before reading counters.
    # Nested calls join the caller's transaction, including registration.
    args = ("IMMEDIATE",) if isinstance(database, SqliteDatabase) and database.transaction_depth() == 0 else ()
    with database.atomic(*args):
        yield


def _locked(query):
    if not isinstance(RepositoryInvite._meta.database, SqliteDatabase):
        query = query.for_update()
    return query.get_or_none()


def _date(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise RepositoryInviteError("A valid expiration time is required.", "invalid_expiration")
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _private_repo(repo: Repository, *, invite_only=False) -> Repository:
    actual = Repository.get_or_none(Repository.id == repo.id)
    if actual is None or not actual.private or (invite_only and not actual.invite_mode):
        raise RepositoryInviteError("This operation requires an invitation repository." if invite_only else "This operation requires a private repository.", "invalid_repository")
    return actual


def _event(repo, action, *, actor=None, user=None, invite=None, request=None, **detail):
    RepositoryAccessEvent.create(
        repository=repo, action=action, actor=actor, subject_user=user,
        invite=invite, access_request=request, detail=json.dumps(detail, sort_keys=True),
    )


def _account(user: User):
    if user is None or user.is_org or not user.is_active:
        raise RepositoryInviteError("A registered personal account is required.", "invalid_account")


def issue_invite(
    repo: Repository, actor: User | None, *, expires_at: datetime,
    max_redemptions: int = 1, max_guest_sessions: int = 1, guest_ttl_seconds: int = 86400,
    grant_expires_at: datetime | None = None, label: str = "",
    access_request: RepositoryAccessRequest | None = None, is_admin: bool = False,
) -> tuple[RepositoryInvite, str]:
    check_repo_manage_permission(repo, actor, is_admin=is_admin)
    expires_at, grant_expires_at = _date(expires_at), _date(grant_expires_at)
    now = utcnow()
    if expires_at is None or expires_at <= now or expires_at > now + timedelta(days=366):
        raise RepositoryInviteError("Exchange expiration must be in the next 366 days.", "invalid_expiration")
    if grant_expires_at is not None and grant_expires_at <= now:
        raise RepositoryInviteError("Account grant expiration must be in the future.", "invalid_expiration")
    # Account binding consumes the code; guest visits are audited but do not consume it.
    limits = ((max_redemptions, 1, 1), (max_guest_sessions, 1, 1), (guest_ttl_seconds, 1, 604800))
    if any(type(value) is not int or not low <= value <= high for value, low, high in limits):
        raise RepositoryInviteError("Invitation limits are out of range.", "invalid_limits")
    if not isinstance(label, str) or len(label) > 100:
        raise RepositoryInviteError("Invitation label must be at most 100 characters.", "invalid_label")
    with _atomic():
        repo = _private_repo(repo)
        if access_request is not None:
            access_request = RepositoryAccessRequest.get_or_none(
                (RepositoryAccessRequest.id == access_request.id)
                & (RepositoryAccessRequest.repository == repo.id)
                & (RepositoryAccessRequest.status == "approved")
                & RepositoryAccessRequest.user.is_null()
            )
            if access_request is None:
                raise RepositoryInviteError("An approved anonymous application for this repository is required.", "invalid_request")
        secret = "repo_" + secrets.token_urlsafe(32)
        row = RepositoryInvite.create(
            repository=repo, token_hash=_hash(secret), label=label, created_by=actor,
            created_at=now, expires_at=expires_at, max_redemptions=max_redemptions,
            max_guest_sessions=max_guest_sessions, guest_ttl_seconds=guest_ttl_seconds,
            grant_expires_at=grant_expires_at, access_request=access_request,
        )
        _event(repo, "invite_issued", actor=actor, invite=row, request=access_request)
    return row, secret


def _invite(repo: Repository, token: str) -> RepositoryInvite:
    if not isinstance(token, str) or not token.startswith("repo_") or len(token) > 256:
        raise RepositoryInviteError("Invalid or unavailable repository invitation.")
    row = _locked(RepositoryInvite.select().where(
        (RepositoryInvite.repository == repo.id) & (RepositoryInvite.token_hash == _hash(token))
    ))
    if row is None:
        raise RepositoryInviteError("Invalid or unavailable repository invitation.")
    return row


def find_invite(token: str) -> RepositoryInvite | None:
    """Resolve a code internally for registration; redemption still checks its lifecycle.

    This is not a public validation endpoint. It intentionally includes expired/revoked
    rows so callers use the same exchange logic and idempotency rather than duplicating it.
    """
    if not isinstance(token, str) or not token.startswith("repo_") or len(token) > 256:
        return None
    return RepositoryInvite.get_or_none(RepositoryInvite.token_hash == _hash(token))


def _available(row: RepositoryInvite):
    if row.revoked_at is not None or row.expires_at <= utcnow():
        raise RepositoryInviteError("Invalid or unavailable repository invitation.")


def _grant(repo, user, actor, *, expires_at=None, source="manual", reset=False):
    grant, created = RepositoryAccessGrant.get_or_create(
        repository=repo, user=user,
        defaults={"granted_by": actor, "expires_at": expires_at, "source": source},
    )
    if not created and reset:
        RepositoryAccessGrant.update(granted_by=actor, expires_at=expires_at, revoked_at=None, source=source).where(
            RepositoryAccessGrant.id == grant.id
        ).execute()
        grant = RepositoryAccessGrant.get_by_id(grant.id)
    return grant


def grant_account(repo, actor, user, *, expires_at=None, source="manual", is_admin=False):
    check_repo_manage_permission(repo, actor, is_admin=is_admin)
    _account(user)
    expires_at = _date(expires_at)
    if expires_at is not None and expires_at <= utcnow():
        raise RepositoryInviteError("Account grant expiration must be in the future.", "invalid_expiration")
    if source not in {"manual", "request", "invite"}:
        raise RepositoryInviteError("Unsupported grant source.", "invalid_source")
    with _atomic():
        grant = _grant(repo, user, actor, expires_at=expires_at, source=source, reset=True)
        _event(repo, "grant_issued", actor=actor, user=user, grant_id=grant.id)
        return grant


def revoke_grant(repo, grant_id, actor, *, is_admin=False):
    check_repo_manage_permission(repo, actor, is_admin=is_admin)
    with _atomic():
        grant = _locked(RepositoryAccessGrant.select().where(
            (RepositoryAccessGrant.id == grant_id) & (RepositoryAccessGrant.repository == repo.id)
        ))
        if grant is None:
            raise RepositoryInviteError("Repository grant not found.", "not_found")
        if grant.revoked_at is None:
            RepositoryAccessGrant.update(revoked_at=utcnow()).where(RepositoryAccessGrant.id == grant.id).execute()
            _event(repo, "grant_revoked", actor=actor, user=grant.user, grant_id=grant.id)
        return RepositoryAccessGrant.get_by_id(grant.id)


def redeem_account(repo, token, user):
    _account(user)
    with _atomic():
        row = _invite(repo, token)
        prior = RepositoryInviteRedemption.get_or_none(
            (RepositoryInviteRedemption.invite == row.id) & (RepositoryInviteRedemption.user == user.id)
        )
        # A repeat acknowledges the original exchange; it never renews or recreates a grant.
        if prior is not None:
            if prior.grant_id is None:
                raise RepositoryInviteError("The original account grant is no longer available.", "grant_inactive")
            return prior.grant
        _private_repo(repo)
        _available(row)
        if row.grant_expires_at is not None and row.grant_expires_at <= utcnow():
            raise RepositoryInviteError("The invitation's account grant has expired.", "grant_inactive")
        consumed = RepositoryInvite.update(redemption_count=RepositoryInvite.redemption_count + 1).where(
            (RepositoryInvite.id == row.id) & RepositoryInvite.revoked_at.is_null()
            & (RepositoryInvite.expires_at > utcnow())
            & (RepositoryInvite.redemption_count == 0)
        ).execute()
        if consumed != 1:
            raise RepositoryInviteError("Invalid or unavailable repository invitation.")
        grant = _grant(repo, user, row.created_by, expires_at=row.grant_expires_at, source="invite")
        if grant.revoked_at is not None or (grant.expires_at is not None and grant.expires_at <= utcnow()):
            raise RepositoryInviteError("A repository manager must renew the account grant.", "grant_inactive")
        RepositoryInviteRedemption.create(invite=row, user=user, grant=grant)
        _event(repo, "invite_redeemed", actor=user, user=user, invite=row, grant_id=grant.id)
        return grant


def revoke_invite(repo, invite_id, actor, *, is_admin=False):
    check_repo_manage_permission(repo, actor, is_admin=is_admin)
    with _atomic():
        row = _locked(RepositoryInvite.select().where(
            (RepositoryInvite.id == invite_id) & (RepositoryInvite.repository == repo.id)
        ))
        if row is None:
            raise RepositoryInviteError("Repository invitation not found.", "not_found")
        now = utcnow()
        RepositoryInvite.update(revoked_at=now).where(
            (RepositoryInvite.id == row.id) & RepositoryInvite.revoked_at.is_null()
        ).execute()
        RepositoryGuestSession.update(revoked_at=now).where(
            (RepositoryGuestSession.invite == row.id) & RepositoryGuestSession.revoked_at.is_null()
        ).execute()
        _event(repo, "invite_revoked", actor=actor, invite=row)
        return RepositoryInvite.get_by_id(row.id)


def submit_request(repo, user=None, *, email=None, message=""):
    if user is not None:
        _account(user)
    if not isinstance(message, str) or len(message) > 4000:
        raise RepositoryInviteError("Application message must be at most 4000 characters.", "invalid_message")
    if user is None:
        try:
            email = validate_email(email or "", check_deliverability=False).normalized
        except EmailNotValidError as error:
            raise RepositoryInviteError("A valid email is required for an anonymous application.", "invalid_email") from error
    else:
        email = user.email
    with _atomic():
        repo = _private_repo(repo, invite_only=True)
        receipt = "receipt_" + secrets.token_urlsafe(32) if user is None else None
        if user is None:
            row = RepositoryAccessRequest.create(
                repository=repo, email=email, message=message, receipt_hash=_hash(receipt),
            )
        else:
            row, created = RepositoryAccessRequest.get_or_create(
                repository=repo, user=user, defaults={"email": email, "message": message},
            )
            if not created:
                return row, None
        _event(repo, "request_submitted", actor=user, user=user, request=row)
        return row, receipt


def get_request_status(repo, user=None, receipt=None):
    query = RepositoryAccessRequest.select().where(RepositoryAccessRequest.repository == repo.id)
    if user is not None:
        owned = query.where(RepositoryAccessRequest.user == user.id).get_or_none()
        if owned is not None:
            return owned
    if isinstance(receipt, str) and 1 <= len(receipt) <= 256:
        return query.where(
            RepositoryAccessRequest.user.is_null()
            & (RepositoryAccessRequest.receipt_hash == _hash(receipt))
        ).get_or_none()
    return None


def review_request(repo, request_id, actor, *, approve, grant_expires_at=None, is_admin=False):
    check_repo_manage_permission(repo, actor, is_admin=is_admin)
    with _atomic():
        row = _locked(RepositoryAccessRequest.select().where(
            (RepositoryAccessRequest.id == request_id) & (RepositoryAccessRequest.repository == repo.id)
        ))
        if row is None:
            raise RepositoryInviteError("Repository application not found.", "not_found")
        status = "approved" if approve else "rejected"
        if row.status != "pending":
            if row.status == status:
                return row
            raise RepositoryInviteError("This application has already been reviewed.", "already_reviewed")
        if approve and row.user_id is not None:
            grant_account(repo, actor, row.user, expires_at=grant_expires_at, source="request", is_admin=is_admin)
        RepositoryAccessRequest.update(status=status, reviewed_at=utcnow(), reviewed_by=actor).where(
            RepositoryAccessRequest.id == row.id
        ).execute()
        _event(repo, "request_" + status, actor=actor, user=row.user, request=row)
        return RepositoryAccessRequest.get_by_id(row.id)


def redeem_guest(repo, token):
    with _atomic():
        _private_repo(repo)
        row = _invite(repo, token)
        _available(row)
        # Legacy schemas constrain the audit count to their old capacity column.
        # Grow that storage capacity under the same row lock; it is no guest quota.
        consumed = RepositoryInvite.update(
            guest_session_count=RepositoryInvite.guest_session_count + 1,
            max_guest_sessions=max(row.max_guest_sessions, row.guest_session_count + 1),
        ).where(
            (RepositoryInvite.id == row.id) & RepositoryInvite.revoked_at.is_null()
            & (RepositoryInvite.expires_at > utcnow())
            & (RepositoryInvite.redemption_count == 0)
        ).execute()
        if consumed != 1:
            raise RepositoryInviteError("Invalid or unavailable repository invitation.")
        secret = "guest_" + secrets.token_urlsafe(32)
        now = utcnow()
        session = RepositoryGuestSession.create(
            repository=repo, invite=row, token_hash=_hash(secret), created_at=now,
            expires_at=now + timedelta(seconds=row.guest_ttl_seconds),
        )
        _event(repo, "guest_session_issued", invite=row, session_id=session.id)
        return session, secret


def get_guest_session(repo, secret):
    if not isinstance(secret, str) or not secret.startswith("guest_") or len(secret) > 256:
        return None
    # Exchange expiration does not shorten an already-issued guest session.
    return (
        RepositoryGuestSession.select(RepositoryGuestSession)
        .join(RepositoryInvite)
        .switch(RepositoryGuestSession).join(Repository)
        .where(
            (RepositoryGuestSession.repository == repo.id)
            & (RepositoryGuestSession.token_hash == _hash(secret))
            & RepositoryGuestSession.revoked_at.is_null()
            & (RepositoryGuestSession.expires_at > utcnow())
            & RepositoryInvite.revoked_at.is_null()
            & (RepositoryInvite.repository == repo.id)
            & (Repository.private == True)
        ).get_or_none()
    )


def revoke_guest_session(repo, session_id, actor, *, is_admin=False):
    check_repo_manage_permission(repo, actor, is_admin=is_admin)
    with _atomic():
        row = _locked(RepositoryGuestSession.select().where(
            (RepositoryGuestSession.id == session_id) & (RepositoryGuestSession.repository == repo.id)
        ))
        if row is None:
            raise RepositoryInviteError("Repository guest session not found.", "not_found")
        RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id == row.id).execute()
        _event(repo, "guest_session_revoked", actor=actor, invite=row.invite, session_id=row.id)
        return RepositoryGuestSession.get_by_id(row.id)
