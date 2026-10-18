"""What a password has to look like when it is set (registration, admin create-user, change, reset).

Until now the server took any string, an empty one included; the web form asked for six characters in the browser only.
The rule applies when a password is next set: existing passwords are not touched and logging in is not checked against it.
"""

from typing import Annotated

from pydantic import AfterValidator

MIN_PASSWORD_LENGTH = 8
# bcrypt looks at no more than the first 72 bytes of its input, so a longer password would be silently cut short
MAX_PASSWORD_BYTES = 72


def check_password(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(value.encode()) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes (fewer characters if some are not ASCII)")
    return value


# a field type for request models: a violation is a 422 like any other invalid field
Password = Annotated[str, AfterValidator(check_password)]
