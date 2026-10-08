#!/usr/bin/env python3
"""Count the places where unit tests fake the ORM, query objects or database calls.

This is a developer tool, not a CI gate. The count is the ratchet described in AGENTS.md:
it may go down, it must not go up. Run it before and after a change and compare.

    python scripts/dev/mock_ratchet.py            # print the counts
    python scripts/dev/mock_ratchet.py --files    # also list the files that match
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = ROOT / "test"

ORM_NAMES = (
    r"Repository|User|UserOrganization|Commit|File|Token|Session|DailyRepoStats|"
    r"RepositoryMetadata|Invitation|Like|Organization|get_or_none|\.select"
)
PATTERNS = {
    "monkeypatch of ORM or query attributes": re.compile(
        rf"monkeypatch\.setattr\([^)]*(?:{ORM_NAMES})"
    ),
    "fake query or field classes": re.compile(
        r"class _Query|class _Field|class _Fake|Fake[A-Za-z]*Model|fake_repo_model|"
        r"select=lambda|get_or_none=|SimpleNamespace\([^)]*select"
    ),
    "mock of a database-looking call": re.compile(
        rf"(?:MagicMock|AsyncMock|mock\.patch)[^\n]*(?:{ORM_NAMES})"
    ),
}


def scan(root: Path = TEST_ROOT):
    counts = {name: 0 for name in PATTERNS}
    matches = {name: [] for name in PATTERNS}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, pattern in PATTERNS.items():
            hits = len(pattern.findall(text))
            if hits:
                counts[name] += hits
                matches[name].append((path.relative_to(ROOT).as_posix(), hits))
    return counts, matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", action="store_true", help="list the matching files")
    args = parser.parse_args()
    counts, matches = scan()
    for name, total in counts.items():
        print(f"{total:5d}  {name}")
    print(f"{sum(counts.values()):5d}  total")
    if args.files:
        for name, rows in matches.items():
            print(f"\n{name}:")
            for path, hits in rows:
                print(f"  {hits:3d}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
