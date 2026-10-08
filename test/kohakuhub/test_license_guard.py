"""No source under src/ may carry the Kohaku Software License.

The dataset viewer was the only component under that separate, non-commercial
license and it is removed. This keeps a copy from slipping back in: a LICENSE
file that names it, or a source file that says it is licensed under it.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
MARKER = "kohaku software license"


def _is_license_file(path: Path) -> bool:
    # LICENSE, LICENSE.md, LICENSE.txt: not a module such as license_card.py
    return path.stem.upper() == "LICENSE" or path.name.upper() == "LICENSE"


def _source_files():
    for path in SRC.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".txt", ".toml", ".yml", ".yaml"}:
            yield path
        elif path.is_file() and _is_license_file(path):
            yield path


def test_sources_exist_to_scan():
    assert any(path.suffix == ".py" for path in _source_files())


def test_no_source_or_license_file_names_the_kohaku_software_license():
    offenders = [
        str(path.relative_to(SRC))
        for path in _source_files()
        if MARKER in path.read_text(encoding="utf-8", errors="ignore").lower()
    ]

    assert offenders == []


def test_no_license_file_other_than_the_project_one_lives_under_src():
    assert [str(p.relative_to(SRC)) for p in SRC.rglob("*") if p.is_file() and _is_license_file(p)] == []
