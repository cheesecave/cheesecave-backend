"""Fail a downstream-library CI cell that proved less than it should.

A case may be skipped only because the installed version cannot read the sampled files itself (the
reason says so); a library that is missing skips for another reason and fails the cell. Enough cases must
also pass, or a cell that skipped nearly everything would look green.

Usage: python scripts/ci/check_downstream.py JUNIT_XML --lib NAME --min-passed N
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET

ALLOWED_SKIP = "the library cannot read the sampled files"


def check(junit_path: str, lib: str, min_passed: int) -> list[str]:
    cases = [
        case
        for case in ET.parse(junit_path).getroot().iter("testcase")
        if f"test_downstream_{lib}" in (case.get("classname") or "")  # the corpus checks are not the library's cases
    ]
    passed = skipped = 0
    problems = []
    for case in cases:
        skip = case.find("skipped")
        if case.find("failure") is not None or case.find("error") is not None:
            continue  # pytest's own exit code reports it
        if skip is None:
            passed += 1
        elif ALLOWED_SKIP in (skip.get("message") or ""):
            skipped += 1
        else:
            problems.append(f"skipped for another reason: {case.get('name')}: {(skip.get('message') or '')[:120]}")
    print(f"passed {passed}, skipped because the version cannot read the files {skipped}")
    if passed < min_passed:
        problems.append(f"only {passed} cases passed, at least {min_passed} are needed")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("junit")
    parser.add_argument("--lib", required=True)
    parser.add_argument("--min-passed", type=int, required=True)
    args = parser.parse_args(argv)
    problems = check(args.junit, args.lib, args.min_passed)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
