"""The board tools and their non-commercial documents are gone; these tests keep them out.

KohakuBoard lives in its own repository under its own terms. This repository ships only
AGPL-3.0 code, so none of its scripts, examples or license documents belong here.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOVED_PATHS = [
    "scripts/build_kohakuboard.py",
    "scripts/deploy_board.py",
    "scripts/deploy_board_integrated.py",
    "scripts/generate_mock_board.py",
    "scripts/generate_mock_board_direct.py",
    "docs/non-commercial",
    "examples/kohakuboard_cifar_training.py",
]


def test_board_scripts_docs_and_examples_are_removed():
    assert [path for path in REMOVED_PATHS if (ROOT / path).exists()] == []


def test_contributing_grants_only_agpl_terms():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "non-commercial license" not in text
    assert "KohakuBoard (Standalone Repository" not in text


def test_scripts_readme_no_longer_describes_board_helpers():
    assert "KohakuBoard integration helpers" not in (ROOT / "scripts/README.md").read_text(encoding="utf-8")
