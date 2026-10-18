"""SQL query validation utilities for admin database viewer."""

import re


# Tables and columns that hold credentials. A query that names one is refused; the values of columns with these names
# are also masked in results (``SELECT *`` names none of them). Defence in depth: the viewer is for looking at data, and
# an administrator can do a great deal through other endpoints.
CREDENTIAL_NAMES = frozenset(
    {
        "password_hash",
        "secret",
        "session_id",
        "token_hash",
        "encrypted_token",
        "lease_token",
        "token",
        "session",
        "userexternaltoken",
        "emailverification",
    }
)

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def find_credential_reference(sql: str) -> str | None:
    """The first credential table or column the query names (literals and case ignored), else None."""
    for name in _IDENTIFIER.findall(_STRING_LITERAL.sub("''", sql)):
        if name.lower() in CREDENTIAL_NAMES:
            return name.lower()
    return None


def validate_readonly_sql(sql: str) -> tuple[bool, str | None]:
    """Validate that SQL query is read-only SELECT query.

    Args:
        sql: SQL query string

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not sql or not sql.strip():
        return False, "Query cannot be empty"

    sql_stripped = sql.strip()
    sql_upper = sql_stripped.upper()

    # Must start with SELECT (allow WITH for CTEs)
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        return False, "Only SELECT queries are allowed (WITH...SELECT for CTEs is ok)"

    # Block dangerous SQL commands (not table/column names)
    # These are typically at the start of a statement or after semicolon
    dangerous_patterns = [
        (r"^\s*UPDATE\b", "UPDATE"),
        (r"^\s*DELETE\b", "DELETE"),
        (r"^\s*DROP\b", "DROP"),
        (r"^\s*ALTER\b", "ALTER"),
        (r"^\s*INSERT\b", "INSERT"),
        (r"^\s*CREATE\b", "CREATE"),
        (r"^\s*TRUNCATE\b", "TRUNCATE"),
        (r"^\s*REPLACE\b", "REPLACE"),
        (r"\bEXEC\b", "EXEC"),
        (r"\bEXECUTE\b", "EXECUTE"),
        (r"^\s*GRANT\b", "GRANT"),
        (r"^\s*REVOKE\b", "REVOKE"),
        (r";\s*COMMIT\b", "COMMIT"),  # Only block COMMIT as SQL command, not table name
        (r"^\s*COMMIT\b", "COMMIT"),
        (r";\s*ROLLBACK\b", "ROLLBACK"),
        (r"^\s*ROLLBACK\b", "ROLLBACK"),
        (r"\bPRAGMA\b", "PRAGMA"),
        (r"\bATTACH\b", "ATTACH"),
        (r"\bDETACH\b", "DETACH"),
    ]

    for pattern, keyword in dangerous_patterns:
        if re.search(pattern, sql_upper):
            return False, f"Keyword '{keyword}' is not allowed"

    # Block multiple statements (semicolon-separated)
    # Allow one trailing semicolon
    semicolons = sql_stripped.count(";")
    if semicolons > 1 or (semicolons == 1 and not sql_stripped.endswith(";")):
        return False, "Multiple statements are not allowed"

    # Block comments that might hide malicious code
    if "--" in sql or "/*" in sql or "*/" in sql:
        return False, "SQL comments are not allowed for security reasons"

    # Check for function calls that could be dangerous
    dangerous_functions = ["LOAD_EXTENSION", "ATTACH_DATABASE"]
    for func in dangerous_functions:
        if func in sql_upper:
            return False, f"Function '{func}' is not allowed"

    credential = find_credential_reference(sql)
    if credential:
        return (
            False,
            f"'{credential}' is not available in the viewer (it holds credentials)",
        )

    return True, None


def get_query_templates() -> list[dict]:
    """Get pre-defined safe query templates.

    Returns:
        List of query templates with name and SQL
    """
    return [
        {
            "name": "Recent Users",
            "description": "Users created in the last 7 days",
            "sql": "SELECT id, username, email, email_verified, created_at\nFROM \"user\"\nWHERE is_org = 0 AND created_at >= date('now', '-7 days')\nORDER BY created_at DESC\nLIMIT 100;",
        },
        {
            "name": "Largest Repositories",
            "description": "Top 20 repositories by total file size",
            "sql": "SELECT \n  r.full_id,\n  r.repo_type,\n  r.namespace,\n  r.used_bytes,\n  r.created_at\nFROM repository r\nORDER BY r.used_bytes DESC\nLIMIT 20;",
        },
        {
            "name": "Most Active Committers",
            "description": "Users with most commits in last 30 days",
            "sql": "SELECT \n  c.username,\n  COUNT(*) as commit_count\nFROM \"commit\" c\nWHERE c.created_at >= date('now', '-30 days')\nGROUP BY c.username\nORDER BY commit_count DESC\nLIMIT 20;",
        },
        {
            "name": "LFS Objects by Size",
            "description": "Largest LFS objects in the system",
            "sql": 'SELECT \n  sha256,\n  size,\n  first_seen_at,\n  last_seen_at\nFROM "lfs_object_history"\nORDER BY size DESC\nLIMIT 50;',
        },
        {
            "name": "Users Over Quota",
            "description": "Users exceeding their storage quotas",
            "sql": 'SELECT \n  username,\n  private_used_bytes,\n  private_quota_bytes,\n  public_used_bytes,\n  public_quota_bytes\nFROM "user"\nWHERE is_org = 0 \n  AND (\n    (private_quota_bytes IS NOT NULL AND private_used_bytes > private_quota_bytes)\n    OR (public_quota_bytes IS NOT NULL AND public_used_bytes > public_quota_bytes)\n  )\nLIMIT 50;',
        },
        {
            "name": "Repository Statistics",
            "description": "Count of repositories by type and visibility",
            "sql": "SELECT \n  repo_type,\n  private as is_private,\n  COUNT(*) as count\nFROM repository\nGROUP BY repo_type, private\nORDER BY repo_type, private;",
        },
        {
            "name": "Files by Type",
            "description": "Count and total size of files by extension",
            "sql": "SELECT \n  SUBSTR(path_in_repo, LENGTH(path_in_repo) - POSITION('.' IN REVERSE(path_in_repo)) + 2) as extension,\n  COUNT(*) as file_count,\n  SUM(size) as total_size,\n  SUM(CASE WHEN lfs = true THEN 1 ELSE 0 END) as lfs_count\nFROM \"file\"\nWHERE is_deleted = false AND path_in_repo LIKE '%.%'\nGROUP BY extension\nORDER BY total_size DESC\nLIMIT 30;",
        },
    ]
