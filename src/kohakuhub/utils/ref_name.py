"""Branch and tag names: what the Hub accepts and how LakeFS is told (#67).

The Hub takes what git takes in a ref name (``feature/x``, ``release/v1.0``). LakeFS takes only letters, digits,
``_`` and ``-`` in a branch or tag id, and no leading ``-``. ``to_lakefs_ref`` / ``from_lakefs_ref`` are an exact,
stateless pair applied in one place, the LakeFS client, so everything above it speaks the Hub's names and nothing
is stored.

A character LakeFS does not take becomes ``_-<hex of its code point>-_`` (``feature/x`` is ``feature_-2F-_x``).
A name LakeFS already takes is unchanged, so every existing branch keeps its id, with one exception: an ``_``
followed by ``-`` is escaped as well (``_-5F-_``), so that the marker never appears unescaped and decoding is
unambiguous. A branch that was made before this with ``_-`` in its name is therefore spelled differently; none is
known.
"""

import re

_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_ESCAPE = re.compile(r"_-([0-9A-F]+)-_")
_MODIFIERS = re.compile(r"(?:[~^]\d*)+$")
_FORBIDDEN = frozenset("~^:?*[\\ ")


def to_lakefs_ref(name: str) -> str:
    """The id LakeFS holds a branch or tag under."""
    out = []
    for i, char in enumerate(name):
        keep = char in _SAFE and not (i == 0 and char == "-")
        if char == "_" and name[i + 1 : i + 2] == "-":
            keep = False
        out.append(char if keep else f"_-{ord(char):X}-_")
    return "".join(out)


def to_lakefs_revision(revision: str) -> str:
    """``to_lakefs_ref`` for a revision that may be an expression: a name, a commit id, or either with ``~2`` / ``^``.

    ``~`` and ``^`` are not allowed in a name (git refuses them), so a trailing run of them is a modifier and
    stays as it is.
    """
    modifiers = _MODIFIERS.search(revision)
    if modifiers is None:
        return to_lakefs_ref(revision)
    return to_lakefs_ref(revision[: modifiers.start()]) + modifiers.group()


def from_lakefs_ref(ref: str) -> str:
    """The Hub's name of a branch or tag LakeFS holds under ``ref``."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), ref)


def is_valid_ref_name(name: str) -> bool:
    """Whether git would take ``name`` as a branch or tag name (``git check-ref-format``)."""
    if not name or name.startswith("-") or name == "@" or name.endswith(".") or "@{" in name or ".." in name:
        return False
    if any(c in _FORBIDDEN or ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


def ref_prefixes(name: str):
    """The directories of a name: ``a/b/c`` is inside ``a`` and ``a/b``."""
    parts = name.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])
