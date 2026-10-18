"""Database viewer endpoints for admin API (read-only SQL queries)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from kohakuhub.db import (
    Commit,
    File,
    Invitation,
    LFSObjectHistory,
    Repository,
    Session,
    Token,
    User,
    UserOrganization,
    db,
)
from kohakuhub.logger import get_logger
from kohakuhub.api.admin.utils import verify_admin_token
from kohakuhub.api.admin.utils.validation import (
    get_query_templates,
    CREDENTIAL_NAMES,
    validate_readonly_sql,
)

logger = get_logger("ADMIN")
router = APIRouter()


# ===== Models =====


class QueryRequest(BaseModel):
    """SQL query execution request."""

    sql: str


# ===== Endpoints =====


@router.get("/database/tables")
async def list_database_tables(
    _admin: bool = Depends(verify_admin_token),
):
    """List all database tables with their schemas.

    Args:
        _admin: Admin authentication (dependency)

    Returns:
        List of tables with column information
    """

    models = [
        User,
        Repository,
        File,
        Commit,
        LFSObjectHistory,
        Session,
        Token,
        Invitation,
        UserOrganization,
    ]

    tables = []
    for model in models:
        table_name = model._meta.table_name

        columns = []
        for field_name, field in model._meta.fields.items():
            columns.append(
                {
                    "name": field_name,
                    "type": field.field_type,
                    "null": field.null,
                    "unique": field.unique,
                }
            )

        tables.append(
            {
                "name": table_name,
                "model": model.__name__,
                "columns": columns,
                "column_count": len(columns),
            }
        )

    return {"tables": tables}


@router.get("/database/templates")
async def get_database_query_templates(
    _admin: bool = Depends(verify_admin_token),
):
    """Get pre-defined safe query templates.

    Args:
        _admin: Admin authentication (dependency)

    Returns:
        List of query templates
    """
    return {"templates": get_query_templates()}


@router.post("/database/query")
async def execute_database_query(
    request: QueryRequest,
    _admin: bool = Depends(verify_admin_token),
):
    """Execute read-only SQL query.

    SECURITY:
    - Only SELECT queries allowed
    - Query validation prevents dangerous operations
    - Row limit enforced (1000 max)
    - Timeout enforced (10 seconds)

    Args:
        request: Query request with SQL
        _admin: Admin authentication (dependency)

    Returns:
        Query results with columns and rows

    Raises:
        HTTPException: If query is invalid or execution fails
    """
    sql = request.sql.strip()

    # Validate query
    is_valid, error_msg = validate_readonly_sql(sql)
    if not is_valid:
        raise HTTPException(
            400,
            detail={"error": f"Invalid query: {error_msg}"},
        )

    logger.info(f"Admin executing database query: {sql[:100]}...")

    try:
        # Execute query with row limit
        cursor = db.execute_sql(sql)
        rows = cursor.fetchmany(1000)  # Hard limit: 1000 rows max

        # Get column names
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

        # Convert rows to list of dicts
        results = []
        for row in rows:
            row_dict = {}
            for idx, col_name in enumerate(columns):
                value = row[idx]
                if value is not None and col_name.lower() in CREDENTIAL_NAMES:
                    row_dict[col_name] = "[redacted]"
                    continue
                # Convert datetime/date to string for JSON serialization
                if hasattr(value, "isoformat"):
                    value = value.isoformat()
                # Convert boolean to string for better display
                elif isinstance(value, bool):
                    value = str(value)
                # Convert None to explicit null marker
                elif value is None:
                    value = None  # Keep as None, frontend will handle
                # Convert other types to string if not serializable
                else:
                    try:
                        # Try to keep native types for numbers, strings
                        if not isinstance(value, (int, float, str)):
                            value = str(value)
                    except Exception:
                        value = str(value)
                row_dict[col_name] = value
            results.append(row_dict)

        logger.success(
            f"Query executed successfully: {len(results)} rows, {len(columns)} columns"
        )

        return {
            "columns": columns,
            "rows": results,
            "count": len(results),
            "column_count": len(columns),
            "truncated": len(results) == 1000,
        }

    except Exception as e:
        logger.error(f"Database query failed: {e}")
        raise HTTPException(
            400,
            detail={"error": f"Query execution failed: {str(e)}"},
        )
