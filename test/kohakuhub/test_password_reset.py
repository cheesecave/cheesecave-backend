"""One-time password reset links: issue, look up, claim once, replace, revoke (real SQL)."""

from datetime import datetime, timedelta, timezone

import pytest

from kohakuhub.auth.utils import hash_token
from kohakuhub.db import PasswordResetToken, User
from kohakuhub.password_reset import (
    DEFAULT_TTL,
    TTL_CHOICES,
    active_link,
    claim,
    find_valid,
    issue_link,
    revoke_links,
    ttl_seconds,
)
from test.kohakuhub.support.factories import make_user


def _past(row, seconds=1):
    PasswordResetToken.update(expires_at=datetime.now(timezone.utc) - timedelta(seconds=seconds)).where(
        PasswordResetToken.id == row.id
    ).execute()


def test_the_choices_run_from_minutes_to_days_and_the_default_is_one_hour():
    assert [(key, seconds) for key, seconds, _ in TTL_CHOICES] == [
        ("15m", 900),
        ("1h", 3600),
        ("6h", 21600),
        ("24h", 86400),
        ("3d", 259200),
        ("7d", 604800),
    ]
    assert DEFAULT_TTL == "1h" and ttl_seconds(DEFAULT_TTL) == 3600
    assert all(label for _, _, label in TTL_CHOICES)


def test_an_unknown_choice_is_an_error_not_a_default():
    with pytest.raises(KeyError):
        ttl_seconds("forever")


def test_a_link_is_stored_as_a_hash_and_the_raw_token_is_returned_once(db_scope):
    user = make_user("alice")

    raw, row = issue_link(user, "6h")

    assert len(raw) == 64 and raw != row.token_hash
    assert row.token_hash == hash_token(raw)
    assert PasswordResetToken.select().where(PasswordResetToken.token_hash == raw).count() == 0
    assert row.created_by == "admin" and row.used_at is None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    assert abs((expires - datetime.now(timezone.utc)).total_seconds() - 21600) < 5


def test_a_new_link_replaces_the_unused_one(db_scope):
    user = make_user("alice")
    first, _ = issue_link(user)

    second, row = issue_link(user, "15m", created_by="self")

    assert find_valid(first) is None
    assert find_valid(second).id == row.id and row.created_by == "self"
    assert PasswordResetToken.select().where(PasswordResetToken.user == user).count() == 1


def test_links_of_different_users_do_not_replace_each_other(db_scope):
    alice, bob = make_user("alice"), make_user("bob")
    a, _ = issue_link(alice)
    b, _ = issue_link(bob)

    assert find_valid(a) is not None and find_valid(b) is not None


def test_an_unknown_token_finds_nothing(db_scope):
    assert find_valid("0" * 64) is None
    assert find_valid("") is None


def test_an_expired_link_is_not_valid_but_is_not_active_either(db_scope):
    user = make_user("alice")
    raw, row = issue_link(user)
    _past(row)

    assert find_valid(raw) is None
    assert active_link(user) is None


def test_a_link_can_be_claimed_exactly_once(db_scope):
    user = make_user("alice")
    raw, _ = issue_link(user)
    row = find_valid(raw)

    assert claim(row) is True
    assert claim(row) is False  # the same row, a second time: the conditional update matches nothing
    assert find_valid(raw) is None
    assert PasswordResetToken.get_by_id(row.id).used_at is not None  # the row stays, as history


def test_a_link_that_expired_after_it_was_found_cannot_be_claimed(db_scope):
    user = make_user("alice")
    raw, _ = issue_link(user)
    row = find_valid(raw)
    _past(row)

    assert claim(row) is False


def test_the_active_link_is_the_unused_unexpired_one(db_scope):
    user = make_user("alice")
    assert active_link(user) is None

    raw, row = issue_link(user)
    assert active_link(user).id == row.id

    claim(find_valid(raw))
    assert active_link(user) is None


def test_revoking_removes_only_the_unused_links_of_that_user(db_scope):
    alice, bob = make_user("alice"), make_user("bob")
    used_raw, used = issue_link(alice)
    claim(find_valid(used_raw))
    raw, _ = issue_link(alice)
    other, _ = issue_link(bob)

    assert revoke_links(alice) == 1
    assert find_valid(raw) is None and find_valid(other) is not None
    assert PasswordResetToken.get_by_id(used.id).used_at is not None
    assert revoke_links(alice) == 0


def test_a_link_goes_with_its_account(db_scope):
    user = make_user("alice")
    issue_link(user)

    User.delete().where(User.id == user.id).execute()

    assert PasswordResetToken.select().count() == 0
