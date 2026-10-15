#!/usr/bin/env python3
"""Count the broad exception handlers in the runtime code (``src/kohakuhub``).

A broad handler (``except Exception``, ``except BaseException`` or a bare ``except``) turns a real failure into
whatever the handler answers: a store that cannot be reached becomes "does not exist", an outage becomes a 404 (#10).
This is a developer tool, not a CI gate. The count is a ratchet like ``mock_ratchet.py``: it may go down, it must not go
up. A new handler that has to be broad (a task boundary, a clean-up that must not mask the real error) says so and why on
its ``except`` line with ``# noqa: BLE001``; those are counted apart.

    python scripts/dev/broad_except_ratchet.py            # print the counts
    python scripts/dev/broad_except_ratchet.py --files    # also list the files, most first
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "kohakuhub"
BROAD = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    kind = handler.type
    if kind is None:
        return True
    names = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    return any(isinstance(n, ast.Name) and n.id in BROAD for n in names)


def scan(root: Path = SRC):
    """``(unmarked, marked, per_file)``: broad handlers without and with ``# noqa: BLE001``, and unmarked by file."""
    unmarked = marked = 0
    per_file: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _is_broad(node):
                if "noqa: BLE001" in lines[node.lineno - 1]:
                    marked += 1
                else:
                    unmarked += 1
                    key = path.relative_to(ROOT).as_posix()
                    per_file[key] = per_file.get(key, 0) + 1
    return unmarked, marked, per_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", action="store_true", help="list the files with unmarked handlers")
    args = parser.parse_args()
    unmarked, marked, per_file = scan()
    print(f"{unmarked:5d}  broad handlers without a reason (`# noqa: BLE001`)")
    print(f"{marked:5d}  broad handlers that say why")
    if args.files:
        print()
        for path, hits in sorted(per_file.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {hits:3d}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
