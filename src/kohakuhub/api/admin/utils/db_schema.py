"""The schema of the database as it is, for the admin database viewer (prototype).

The truth is the live database (peewee's introspection), not a list somebody keeps: a new table appears here without
anybody touching this file. The models of ``kohakuhub.db`` are laid over it for the description, and the two are
compared, so a model without a table (a migration did not run) and a table without a model (``schema_rollout``) show.
Only the *group* of a table is written down (``GROUPS``); a table that is in none of them is listed as "Other" and a test
asks for it to be put in one.
"""

from __future__ import annotations

import inspect

from peewee import Model

from kohakuhub import db as db_module
from kohakuhub.api.admin.utils.validation import CREDENTIAL_NAMES

# table -> group; the order of the groups is the order of the viewer
GROUPS: list[tuple[str, list[str]]] = [
    ("Accounts", ["user", "userorganization", "user_follow", "user_preference", "sshkey", "userexternaltoken",
                  "invitation", "emailverification", "confirmationtoken", "password_reset_token", "session", "token"]),
    ("Repositories", ["repository", "repository_card", "repository_creator", "repository_facet", "repository_metadata",
                      "repository_write", "repositorylike", "commit", "path_commit", "file", "branch_commit_lock",
                      "dailyrepostats", "downloadsession"]),
    ("Storage and LFS", ["lfsobjecthistory", "lfs_gc_candidate", "lfs_gc_state", "lfs_grant", "lfs_head_ref",
                         "lfs_object_tombstone", "lfs_recent_object", "stagingupload"]),
    ("Background tasks", ["background_task", "background_task_event", "background_task_log", "background_worker"]),
    ("Collections", ["collection", "collectionitem", "collectionupvote"]),
    ("Site and admin", ["admin_auth_client", "admin_session", "fallbacksource", "site_appearance", "site_branding",
                        "site_homepage", "schema_rollout"]),
]
GROUP_OF = {table: group for group, tables in GROUPS for table in tables}

# Columns that are about people (shown with a warning), as opposed to credentials (never returned)
PII_NAMES = frozenset({"email", "ip", "user_agent", "client_key", "full_name"})


def column_kind(name: str) -> str | None:
    lowered = name.lower()
    if lowered in CREDENTIAL_NAMES:
        return "credential"
    if lowered in PII_NAMES:
        return "pii"
    return None


def current_db():
    """The database the models are bound to (the one a test binds, else the application's)."""
    return db_module.User._meta.database


def _schema():
    return db_module.User._meta.schema or "public"


def models_by_table() -> dict[str, type[Model]]:
    found = {}
    for _name, cls in inspect.getmembers(db_module, inspect.isclass):
        if issubclass(cls, Model) and cls is not Model and cls.__module__ == db_module.__name__:
            found[cls._meta.table_name] = cls
    return found


def credential_tables() -> set[str]:
    """Tables with a credential column: a whole-row reference to one of them would carry it out."""
    return {
        table
        for table, model in models_by_table().items()
        if any(name.lower() in CREDENTIAL_NAMES for name in model._meta.columns)
    }


def _estimates() -> dict[str, tuple[int, int]]:
    if not _is_postgres():
        return {}
    rows = current_db().execute_sql(
        "SELECT c.relname, c.reltuples::bigint, pg_total_relation_size(c.oid) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'",
        (_schema(),),
    ).fetchall()
    return {name: (int(rows_), int(size)) for name, rows_, size in rows}


def _is_postgres() -> bool:
    return current_db().__class__.__name__.lower().startswith("postgres")


def build_schema() -> dict:
    models = models_by_table()
    estimates = _estimates()
    db = current_db()
    schema = _schema() if _is_postgres() else None
    tables = []
    for name in sorted(db.get_tables(schema)):
        foreign = {fk.column: {"table": fk.dest_table, "column": fk.dest_column} for fk in db.get_foreign_keys(name, schema)}
        primary = set(db.get_primary_keys(name, schema))
        columns = [
            {
                "name": col.name,
                "type": str(col.data_type).lower(),
                "null": bool(col.null),
                "primary_key": col.name in primary,
                "references": foreign.get(col.name),
                "kind": column_kind(col.name),
            }
            for col in db.get_columns(name, schema)
        ]
        indexes = [{"name": ix.name, "columns": list(ix.columns), "unique": bool(ix.unique)} for ix in db.get_indexes(name, schema)]
        model = models.get(name)
        doc = (inspect.getdoc(model) or "").strip().split("\n")[0].replace("``", "") if model else ""
        est = estimates.get(name)
        tables.append(
            {
                "name": name,
                "group": GROUP_OF.get(name, "Other"),
                "model": model.__name__ if model else None,
                "description": doc or None,
                "columns": columns,
                "column_count": len(columns),
                "indexes": indexes,
                # -1: PostgreSQL has not analysed the table yet; None: the engine has no estimate (SQLite)
                "estimated_rows": est[0] if est else None,
                "total_bytes": est[1] if est else None,
            }
        )
    present = {t["name"] for t in tables}
    return {
        "engine": "postgresql" if _is_postgres() else "sqlite",
        "groups": [g for g, _ in GROUPS] + (["Other"] if any(t["group"] == "Other" for t in tables) else []),
        "tables": tables,
        "drift": {
            "tables_without_model": sorted(t["name"] for t in tables if t["model"] is None),
            "models_without_table": sorted(n for n in models if n not in present and n != "basemodel"),
            "tables_without_group": sorted(t["name"] for t in tables if t["group"] == "Other"),
        },
    }
