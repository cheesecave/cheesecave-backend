"""Environment variable names: ``CHEESE_CAVE_<NAME>`` first, ``KOHAKU_HUB_<NAME>`` as the fallback.

Every setting can be given under either prefix. For one setting the new prefix wins when
both are set; an empty value counts as set (the same as ``NAME in os.environ``). The old
prefix stays supported for deployments written before the rename.

This module has no side effects on import and no dependencies, so the container entrypoint
and the scripts can use it without loading (and validating) the application's configuration,
which ``kohakuhub.config`` does as soon as it is imported.
"""

import os
from collections.abc import Mapping

ENV_PREFIXES = ("CHEESE_CAVE_", "KOHAKU_HUB_")

# Names the system itself writes under the old prefix: the LakeFS credentials file is a data
# format other tools and older images read, and an image build declares both names of its
# identity. They are not the operator's to rename, so they are not advised about.
SYSTEM_NAMES = frozenset(
    {"LAKEFS_ACCESS_KEY", "LAKEFS_SECRET_KEY", "GIT_SHA", "BUILD_TIME"}
)


def read_env(name: str) -> str | None:
    """Return the value of CHEESE_CAVE_<name>, else KOHAKU_HUB_<name>, else None.

    An empty value still counts as set, the same as the existing ``in os.environ`` checks.
    """
    for prefix in ENV_PREFIXES:
        if prefix + name in os.environ:
            return os.environ[prefix + name]
    return None


def has_env(name: str) -> bool:
    """Whether <name> is set under either prefix (an empty value counts)."""
    return read_env(name) is not None


def env_or(name: str, default: str) -> str:
    """The value of <name> under either prefix, ``default`` only when it is set under neither."""
    value = read_env(name)
    return default if value is None else value


def read_env_nonempty(name: str) -> str | None:
    """The first non-empty value of <name>, new prefix first.

    For what an image build declares under both names: the one that was not passed is
    empty rather than absent, and must not hide the one that was.
    """
    for prefix in ENV_PREFIXES:
        if os.environ.get(prefix + name):
            return os.environ[prefix + name]
    return None


def legacy_names(environ: Mapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """The old-prefix variables set in ``environ``, split by whether they still take effect.

    Returns ``(in_use, shadowed)``: ``in_use`` are old names with no new name beside them
    (their value is what the application reads); ``shadowed`` are old names whose new name
    is also set (the old one is ignored). Names only, sorted; never values.
    """
    environ = os.environ if environ is None else environ
    new, old = ENV_PREFIXES
    in_use, shadowed = [], []
    for key in sorted(environ):
        if not key.startswith(old):
            continue
        name = key[len(old) :]
        if name in SYSTEM_NAMES:
            continue
        (shadowed if new + name in environ else in_use).append(key)
    return in_use, shadowed


def legacy_advice(environ: Mapping[str, str] | None = None) -> list[str]:
    """Log lines advising to rename old-prefix variables; empty when there is nothing to say."""
    in_use, shadowed = legacy_names(environ)
    new, old = ENV_PREFIXES
    lines = []
    if in_use:
        lines.append(
            f"{len(in_use)} environment variable(s) still use the old {old} prefix and are "
            f"read as a fallback; rename them when convenient: "
            + ", ".join(f"{key} -> {new}{key[len(old):]}" for key in in_use)
        )
    if shadowed:
        lines.append(
            f"{len(shadowed)} environment variable(s) are set under both prefixes; "
            f"{new}<NAME> wins and the {old}<NAME> value is ignored: " + ", ".join(shadowed)
        )
    return lines
