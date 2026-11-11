"""The reports of the admin database viewer: queries that ship with the code (prototype).

One YAML file each in ``api/admin/reports``: an id, a title, a category, a description, the parameters with their
types and bounds, ``heavy`` for a query that scans a big table, the engines it is written for, and the SQL, which
names its parameters ``:name``. A test runs every report on every engine it names, so a report cannot rot silently
(five of the seven templates it replaces did not run on the production engine).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"
CATEGORIES = ["Health and capacity", "Accounts", "Repositories", "Storage and LFS", "Product", "Security and site"]


class ReportError(ValueError):
    """A parameter that is missing, of the wrong type or out of bounds."""


@lru_cache(maxsize=1)
def load_reports() -> tuple[dict, ...]:
    reports = [yaml.safe_load(path.read_text()) for path in sorted(REPORT_DIR.glob("*.yml"))]
    return tuple(sorted(reports, key=lambda r: (CATEGORIES.index(r["category"]), r["title"])))


def get_report(report_id: str) -> dict | None:
    return next((r for r in load_reports() if r["id"] == report_id), None)


def bind_parameters(report: dict, supplied: dict | None) -> dict:
    """The values of the parameters of ``report``: what was supplied, else the default, each checked."""
    supplied = supplied or {}
    known = {p["name"] for p in report.get("params", [])}
    if extra := sorted(set(supplied) - known):
        raise ReportError(f"Unknown parameter: {', '.join(extra)}")
    values = {}
    for param in report.get("params", []):
        name, kind = param["name"], param["type"]
        value = supplied.get(name, param.get("default"))
        if value is None:
            raise ReportError(f"Parameter '{name}' is required")
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).lstrip("-").isdigit():
                raise ReportError(f"Parameter '{name}' must be a whole number")
            value = int(value)
            low, high = param.get("min"), param.get("max")
            if (low is not None and value < low) or (high is not None and value > high):
                raise ReportError(f"Parameter '{name}' must be between {low} and {high}")
        elif kind == "enum":
            if value not in param["choices"]:
                raise ReportError(f"Parameter '{name}' must be one of: {', '.join(param['choices'])}")
        elif kind == "text":
            value = str(value)
        else:
            raise ReportError(f"Parameter '{name}' has an unknown type {kind}")
        values[name] = value
    return values
