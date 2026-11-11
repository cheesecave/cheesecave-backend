"""Database viewer endpoints for the admin API (read-only; prototype).

``/database/schema`` is the live database, ``/database/reports`` the queries that ship with the code,
``/database/query`` a free query, all run through ``db_exec.run_readonly``. ``/database/tables`` and
``/database/templates`` keep their old shapes for the admin build that is already deployed.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kohakuhub.api.admin.utils import verify_admin_token
from kohakuhub.api.admin.utils.db_exec import (
    DISPLAY_ROWS,
    EXPORT_ROWS,
    QueryRefused,
    QueryTimeout,
    run_readonly,
)
from kohakuhub.api.admin.utils.db_reports import (
    CATEGORIES,
    ReportError,
    bind_parameters,
    get_report,
    load_reports,
)
from kohakuhub.api.admin.utils.db_schema import _is_postgres, build_schema
from kohakuhub.logger import get_logger

logger = get_logger("ADMIN")
router = APIRouter()


class QueryRequest(BaseModel):
    """SQL query execution request."""

    sql: str
    explain: bool = False
    # a bigger cap for an export; never more than EXPORT_ROWS
    max_rows: int = Field(DISPLAY_ROWS, ge=1, le=EXPORT_ROWS)


class ReportRunRequest(BaseModel):
    params: dict = {}
    explain: bool = False
    max_rows: int = Field(DISPLAY_ROWS, ge=1, le=EXPORT_ROWS)


def _run(sql, params=None, *, explain=False, max_rows=DISPLAY_ROWS, trusted=False):
    try:
        return run_readonly(sql, params, explain=explain, max_rows=max_rows, check=not trusted)
    except QueryRefused as err:
        raise HTTPException(400, detail={"error": str(err), "code": "refused"})
    except QueryTimeout as err:
        raise HTTPException(408, detail={"error": str(err), "code": "timeout"})
    except Exception as err:  # the database's own message (a typo in a column name) is what the admin needs
        logger.error(f"Database query failed: {err}")
        raise HTTPException(400, detail={"error": f"Query execution failed: {err}", "code": "failed"})


@router.get("/database/schema")
async def database_schema(_admin: bool = Depends(verify_admin_token)):
    """The tables the database has now, grouped, with columns, keys, indexes, estimates and drift."""
    return build_schema()


@router.get("/database/tables")
async def list_database_tables(_admin: bool = Depends(verify_admin_token)):
    """Old shape of the table list, built from the live schema (credential tables are left out)."""
    from kohakuhub.api.admin.utils.db_schema import credential_tables

    hidden = credential_tables()
    tables = [
        {
            "name": t["name"],
            "model": t["model"],
            "columns": [{"name": c["name"], "type": c["type"], "null": c["null"], "unique": c["primary_key"]} for c in t["columns"]],
            "column_count": t["column_count"],
        }
        for t in build_schema()["tables"]
        if t["name"] not in hidden
    ]
    return {"tables": tables}


@router.get("/database/reports")
async def list_reports(_admin: bool = Depends(verify_admin_token)):
    engine = "postgresql" if _is_postgres() else "sqlite"
    reports = [
        {k: r[k] for k in ("id", "title", "category", "description", "heavy", "params")}
        for r in load_reports()
        if engine in r["dialects"]
    ]
    return {"categories": CATEGORIES, "reports": reports, "engine": engine}


@router.get("/database/templates")
async def get_database_query_templates(_admin: bool = Depends(verify_admin_token)):
    """Old shape of the templates: the reports with their default parameters filled in."""
    out = []
    for r in (await list_reports())["reports"]:
        out.append({"name": r["title"], "description": r["description"], "report": r["id"]})
    return {"templates": out}


@router.post("/database/reports/{report_id}/run")
async def run_report(report_id: str, request: ReportRunRequest, _admin: bool = Depends(verify_admin_token)):
    report = get_report(report_id)
    if report is None:
        raise HTTPException(404, detail={"error": f"No report '{report_id}'"})
    try:
        values = bind_parameters(report, request.params)
    except ReportError as err:
        raise HTTPException(400, detail={"error": str(err), "code": "parameters"})
    # a report ships with the code and only takes bound values: it is trusted, the database's own limits still apply
    result = _run(report["sql"], values, explain=request.explain, max_rows=request.max_rows, trusted=True)
    return {**result, "report": report_id, "params": values}


@router.post("/database/query")
async def execute_database_query(request: QueryRequest, _admin: bool = Depends(verify_admin_token)):
    """Run a read-only query. The guard gives the clear message; the database enforces the limits."""
    logger.info(f"Admin executing database query: {request.sql[:100]}...")
    return _run(request.sql, None, explain=request.explain, max_rows=request.max_rows)
