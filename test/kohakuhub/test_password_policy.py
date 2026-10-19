"""The server-side password policy: it did not exist (registration and admin create-user took any string)."""

import pytest
from pydantic import BaseModel, ValidationError

from kohakuhub.password_policy import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    Password,
    check_password,
)


def test_the_limits_are_the_documented_ones():
    assert MIN_PASSWORD_LENGTH == 8
    assert MAX_PASSWORD_BYTES == 72  # bcrypt looks at no more than the first 72 bytes


@pytest.mark.parametrize("value", ["12345678", "correct horse battery staple", "a" * 72, "パスワード" * 4, " " * 8])
def test_a_password_inside_the_limits_passes_unchanged(value):
    assert check_password(value) == value


@pytest.mark.parametrize("value", ["", "short", "1234567"])
def test_a_password_below_the_minimum_is_refused(value):
    with pytest.raises(ValueError, match="at least 8 characters"):
        check_password(value)


@pytest.mark.parametrize("value", ["a" * 73, "パスワード" * 5, "é" * 37])
def test_a_password_above_72_bytes_is_refused_even_when_it_has_fewer_characters(value):
    # the length of "パスワード" * 5 is 25 characters but 75 bytes
    with pytest.raises(ValueError, match="at most 72 bytes"):
        check_password(value)


def test_the_type_applies_the_policy_in_a_pydantic_model():
    class Body(BaseModel):
        password: Password

    assert Body(password="longenough").password == "longenough"
    with pytest.raises(ValidationError):
        Body(password="short")
    with pytest.raises(ValidationError):
        Body(password="a" * 73)
