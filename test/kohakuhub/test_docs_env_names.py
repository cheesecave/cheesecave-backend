"""The documentation and examples name settings CHEESE_CAVE_*.

``KOHAKU_HUB_<NAME>`` still works as a fallback, so a document may mention it, but only to say so:
the line has to speak about the old prefix. Anything else that names a setting with the old prefix
is a stale example. Files that are history on purpose (the changelog, ``provenance/``) and the CI
workflows (which exercise the fallback) are not checked.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CHECKED = (
    [*ROOT.glob("docs/**/*.md")]
    + [*ROOT.glob("src/kohakuhub/**/README.md")]
    + [ROOT / name for name in ("README.md", "README.zh-CN.md", "BRANDING.md", "AGENTS.md")]
    + [ROOT / name for name in (".env.dev.example", ".env.compose.example")]
)

# A line may name the old prefix when it is about the old prefix.
ABOUT_THE_OLD_PREFIX = re.compile(
    r"older|\bold\b|fallback|legacy|compatib|credentials file|still (works|accepted|read)|"
    r"KOHAKU_HUB_<NAME>|KOHAKU_HUB_\*|旧|回退|既有|仍",
    re.IGNORECASE,
)


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: str(p.relative_to(ROOT)))
def test_old_prefixed_names_appear_only_to_explain_the_fallback(path):
    stale = [
        f"{number}: {line.strip()[:120]}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "KOHAKU_HUB_" in line and not ABOUT_THE_OLD_PREFIX.search(line)
    ]
    assert stale == []


def test_the_configuration_reference_explains_the_two_prefixes():
    text = (ROOT / "docs" / "reference" / "config.md").read_text(encoding="utf-8")
    assert "CHEESE_CAVE_<NAME>" in text and "KOHAKU_HUB_<NAME>" in text
    assert "wins" in text


def test_the_env_examples_use_the_new_prefix_only():
    for name in (".env.dev.example", ".env.compose.example"):
        values = [
            line
            for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
            if re.match(r"#?\s*[A-Z0-9_]+=", line)
        ]
        assert values and not [line for line in values if "KOHAKU_HUB_" in line], name
        assert any(line.lstrip("# ").startswith("CHEESE_CAVE_") for line in values), name
