"""The data explorer's server side: bounded, cacheable reads of tabular files and archives (layers L2 and L3).

Everything here is a *fixed, parameterised* operation over a repository file: read a page of rows, count, scan with a
restricted predicate, summarise from the footer, walk a tar's headers. There is no arbitrary SQL on the server (that is
layer L4, and it runs in the browser). Every read goes through ``RangeFile`` so it is counted and budgeted, and every
summary is cached under the file's content hash (the ETag of an LFS object is its sha256), so the same object is
summarised once however many repositories, branches or revisions refer to it.
"""

import asyncio
import base64
import datetime as dt
import io
import json
import re
import tarfile
import threading
import time
from dataclasses import dataclass

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.json as pajson
import pyarrow.parquet as pq

from kohakuhub.api.files import _get_file_metadata
from kohakuhub.api.viewer import splits as splits_mod
from kohakuhub.api.viewer.rangefile import BudgetExceeded, RangeFile
from kohakuhub.auth.permissions import check_repo_read_permission
from kohakuhub.db_operations import get_repository
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo, resolve_revision

MAX_ROWS = 100                    # rows per request, as on the Hub
MAX_CELL_CHARS = 1000             # a longer string is cut and its column named in truncated_cells
SCAN_BUDGET = 256 * 1024 * 1024   # bytes one search/filter/statistics request may read
PREVIEW_BYTES = 4 * 1024 * 1024   # how much of a csv/jsonl file the preview reads
STATS_SAMPLE_ROWS = 50_000
TAR_MAX_MEMBERS = 200_000
TAR_TIME_LIMIT = 30.0

METRICS = {"cache_hits": 0, "cache_misses": 0}
_CACHE: dict[tuple, object] = {}
_LOCK = threading.Lock()


def cached(key: tuple, build):
    """Build once per key (the key carries a content hash, so a hit is always valid)."""
    with _LOCK:
        if key in _CACHE:
            METRICS["cache_hits"] += 1
            return _CACHE[key]
    value = build()
    with _LOCK:
        METRICS["cache_misses"] += 1
        _CACHE[key] = value
    return value


# ------------------------------------------------------------------------------------------------ repository access
@dataclass
class RepoCtx:
    repo_type: str
    namespace: str
    name: str
    repo: object
    lakefs_repo: str
    client: object
    commit: str
    revision: str
    user: object


@dataclass
class FileRef:
    path: str
    url: str
    size: int
    sha: str
    commit: str


async def open_repo(repo_type: str, namespace: str, name: str, revision: str, user) -> RepoCtx:
    from fastapi import HTTPException

    repo = get_repository(repo_type, namespace, name)
    if repo is None:
        raise HTTPException(404, detail={"error": f"Repository '{namespace}/{name}' not found"}, headers={"X-Error-Code": "RepoNotFound"})
    check_repo_read_permission(repo, user)   # the global handler turns a denial into the Hub's 404/401
    lakefs_repo = resolve_lakefs_repo(repo)
    client = get_lakefs_client()
    try:
        commit, _ = await resolve_revision(client, lakefs_repo, revision, repo)
    except ValueError as e:
        raise HTTPException(404, detail={"error": str(e)}, headers={"X-Error-Code": "RevisionNotFound"}) from e
    return RepoCtx(repo_type, namespace, name, repo, lakefs_repo, client, commit, revision, user)


async def list_files(ctx: RepoCtx, prefix: str = "") -> list[dict]:
    out, after = [], ""
    while True:
        page = await ctx.client.list_objects(ctx.lakefs_repo, ctx.commit, prefix=prefix, after=after, amount=1000)
        out += [{"path": o["path"], "size": o.get("size_bytes", 0)} for o in page["results"] if o.get("path_type", "object") == "object"]
        if not page["pagination"]["has_more"]:
            return out
        after = page["pagination"]["next_offset"]


async def read_text(ctx: RepoCtx, path: str, limit: int = 1_000_000) -> str | None:
    try:
        data = await ctx.client.get_object(ctx.lakefs_repo, ctx.commit, path, range_header=f"bytes=0-{limit - 1}")
    except Exception:
        return None
    return data.decode("utf-8", "replace")


async def locate(ctx: RepoCtx, path: str, revision: str | None = None) -> FileRef:
    """Where a file's bytes are, through the same code (and the same permission check) as ``/resolve``."""
    url, headers = await _get_file_metadata(ctx.repo_type, ctx.namespace, ctx.name, revision or ctx.commit, path, ctx.user)
    return FileRef(path, url, int(headers["Content-Length"]), headers.get("ETag") or f"{ctx.commit}:{path}", headers.get("X-Repo-Commit", ""))


async def aio(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ------------------------------------------------------------------------------------------------ parquet summaries
def _stat_json(v):
    if isinstance(v, (bytes, bytearray)):
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (np.generic,)):
        return v.item()
    return v


def _feature_of(field: pa.Field, hf: dict | None) -> dict:
    if hf and field.name in hf:
        return hf[field.name]
    t = field.type
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return {"feature": _feature_of(pa.field("item", t.value_type), None), "_type": "List"}
    if pa.types.is_struct(t) and {f.name for f in t} >= {"bytes", "path"}:
        return {"_type": "Image"} if False else {"dtype": "struct<bytes,path>", "_type": "Value"}
    return {"dtype": str(t), "_type": "Value"}


def parquet_summary(ref: FileRef) -> dict:
    """Schema, row groups and column statistics of one parquet file, from its footer only."""

    def build():
        f = RangeFile(ref.url, ref.size)
        pf = pq.ParquetFile(f)
        md = pf.metadata
        kv = md.metadata or {}
        hf = None
        if b"huggingface" in kv:
            try:
                hf = json.loads(kv[b"huggingface"])["info"]["features"]
            except (ValueError, KeyError):
                hf = None
        schema = pf.schema_arrow
        columns = [{"name": fld.name, "type": str(fld.type), "feature": _feature_of(fld, hf)} for fld in schema]
        groups = []
        for i in range(md.num_row_groups):
            rg = md.row_group(i)
            stats = {}
            for j in range(rg.num_columns):
                col = rg.column(j)
                s = col.statistics
                name = col.path_in_schema
                if s is not None and "." not in name:
                    stats[name] = {"min": _stat_json(s.min) if s.has_min_max else None, "max": _stat_json(s.max) if s.has_min_max else None,
                                   "nulls": s.null_count if s.has_null_count else None}
            groups.append({"rows": rg.num_rows, "bytes": rg.total_byte_size, "stats": stats})
        return {"rows": md.num_rows, "row_groups": groups, "columns": columns, "features": hf, "created_by": md.created_by,
                "footer_requests": f.requests, "footer_bytes": f.bytes, "file_size": ref.size}

    return cached(("parquet-summary", ref.sha), build)


def read_row_groups(ref: FileRef, indices: list[int], columns: list[str] | None = None, budget: int | None = None) -> tuple[pa.Table, RangeFile]:
    f = RangeFile(ref.url, ref.size, budget=budget)
    pf = pq.ParquetFile(f)
    return pf.read_row_groups(indices, columns=columns), f


def cell(value, max_chars: int = MAX_CELL_CHARS):
    """One value as JSON: bytes are described not sent, long strings are cut (the caller records which column)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (np.generic,)):
        return value.item()
    if isinstance(value, (bytes, bytearray)):
        return {"_type": "bytes", "len": len(value)}
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars]
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dict):
        if {"bytes", "path"} <= set(value) and isinstance(value.get("bytes"), (bytes, bytearray, type(None))):
            return {"_type": "binary-struct", "path": value["path"], "len": len(value["bytes"] or b"")}
        return {k: cell(v, max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [cell(v, max_chars) for v in value[:100]]
    return str(value)


def table_rows(table: pa.Table, first_idx: int) -> list[dict]:
    rows = []
    cols = table.column_names
    pyrows = table.to_pylist()
    for i, r in enumerate(pyrows):
        truncated = [c for c in cols if isinstance(r[c], str) and len(r[c]) > MAX_CELL_CHARS]
        rows.append({"row_idx": first_idx + i, "row": {c: cell(r[c]) for c in cols}, "truncated_cells": truncated})
    return rows


def features_list(columns: list[dict]) -> list[dict]:
    return [{"feature_idx": i, "name": c["name"], "type": c["feature"]} for i, c in enumerate(columns)]


# ------------------------------------------------------------------------------------------------ rows (parquet and text formats)
def _slice_parquet(refs: list[FileRef], offset: int, length: int) -> dict:
    summaries = [parquet_summary(r) for r in refs]
    total = sum(s["rows"] for s in summaries)
    rows, requests, nbytes = [], 0, 0
    base = 0
    want_end = min(offset + length, total)
    for ref, s in zip(refs, summaries):
        file_start, file_end = base, base + s["rows"]
        base = file_end
        if file_end <= offset or file_start >= want_end:
            continue
        first, last = max(offset, file_start) - file_start, min(want_end, file_end) - file_start   # rows of this file
        cursor, picked = 0, []
        for i, g in enumerate(s["row_groups"]):
            if cursor + g["rows"] > first and cursor < last:
                picked.append((i, cursor))
            cursor += g["rows"]
        table, f = read_row_groups(ref, [i for i, _ in picked])
        requests += f.requests
        nbytes += f.bytes
        skip = first - picked[0][1]
        table = table.slice(skip, last - first)
        rows += table_rows(table, file_start + first)
    return {"rows": rows, "total": total, "requests": requests, "bytes": nbytes, "columns": summaries[0]["columns"] if summaries else []}


def _text_prefix(ref: FileRef, limit: int) -> tuple[bytes, RangeFile]:
    f = RangeFile(ref.url, ref.size)
    data = f.read(min(limit, ref.size))
    if ref.size > limit:                      # cut at the last complete line
        data = data[: data.rfind(b"\n") + 1] or data
    return data, f


def _text_table(ref: FileRef, fmt: str, limit: int) -> tuple[pa.Table, bool, RangeFile]:
    data, f = _text_prefix(ref, limit)
    partial = ref.size > limit
    if fmt == "csv":
        sep = "\t" if ref.path.lower().endswith(".tsv") else ","
        table = pacsv.read_csv(io.BytesIO(data), parse_options=pacsv.ParseOptions(delimiter=sep))
    else:
        table = pajson.read_json(io.BytesIO(data))
    return table, partial, f


def _slice_text(ref: FileRef, fmt: str, offset: int, length: int) -> dict:
    table, partial, f = _text_table(ref, fmt, PREVIEW_BYTES)
    est = int(table.num_rows * ref.size / max(1, f.bytes)) if partial else table.num_rows
    page = table.slice(offset, length)
    columns = [{"name": fld.name, "type": str(fld.type), "feature": _feature_of(fld, None)} for fld in table.schema]
    return {"rows": table_rows(page, offset), "total": est if partial else table.num_rows, "partial": partial, "loaded_rows": table.num_rows,
            "requests": f.requests, "bytes": f.bytes, "columns": columns}


def rows(fmt: str, refs: list[FileRef], offset: int, length: int) -> dict:
    length = max(1, min(length, MAX_ROWS))
    if fmt == "parquet":
        r = _slice_parquet(refs, offset, length)
        r["partial"] = False
        r["loaded_rows"] = r["total"]
        return r
    if fmt in ("csv", "jsonl", "json"):
        return _slice_text(refs[0], "csv" if fmt == "csv" else "json", offset, length)
    raise ValueError(f"the explorer cannot page {fmt!r} files")


# ------------------------------------------------------------------------------------------------ filter and search
class FilterError(ValueError):
    """The where clause is not in the supported grammar."""


_TOKEN = re.compile(r"""\s*(?:(?P<ident>"(?:[^"]|"")+")|(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|(?P<op><>|>=|<=|=|>|<)|(?P<kw>AND|OR|NOT)\b|(?P<paren>[()])|(?P<bool>true|false)\b)""", re.I)


def tokenize(where: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    where = where.strip()
    while pos < len(where):
        m = _TOKEN.match(where, pos)
        if not m or m.end() == pos:
            raise FilterError(f"cannot read the where clause at character {pos}: {where[pos:pos + 12]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def parse_where(where: str):
    """where := or ; or := and (OR and)* ; and := not (AND not)* ; not := NOT not | '(' or ')' | "col" op literal"""
    tokens = tokenize(where)
    i = 0

    def peek():
        return tokens[i] if i < len(tokens) else (None, None)

    def take():
        nonlocal i
        i += 1
        return tokens[i - 1]

    def p_or():
        node = p_and()
        while peek()[0] == "kw" and peek()[1].upper() == "OR":
            take(); node = ("or", node, p_and())
        return node

    def p_and():
        node = p_not()
        while peek()[0] == "kw" and peek()[1].upper() == "AND":
            take(); node = ("and", node, p_not())
        return node

    def p_not():
        kind, val = peek()
        if kind == "kw" and val.upper() == "NOT":
            take(); return ("not", p_not())
        if kind == "paren" and val == "(":
            take(); node = p_or()
            if peek() != ("paren", ")"):
                raise FilterError("a parenthesis is not closed")
            take(); return node
        if kind != "ident":
            raise FilterError('a condition starts with a column name in double quotes, for example "age">30')
        col = take()[1][1:-1].replace('""', '"')
        if peek()[0] != "op":
            raise FilterError(f"after the column {col!r} comes a comparison: = <> > >= < <=")
        op = take()[1]
        kind, lit = peek()
        if kind not in ("str", "num", "bool"):
            raise FilterError("a comparison needs a value: 'text', a number, true or false")
        take()
        value = lit[1:-1].replace("''", "'") if kind == "str" else (float(lit) if kind == "num" and re.search(r"[.eE]", lit) else int(lit) if kind == "num" else lit.lower() == "true")
        return ("cmp", col, op, value)

    node = p_or()
    if i != len(tokens):
        raise FilterError(f"unexpected {tokens[i][1]!r}")
    return node


def eval_where(node, table: pa.Table) -> pa.Array:
    kind = node[0]
    if kind == "cmp":
        _, col, op, value = node
        if col not in table.column_names:
            raise FilterError(f"there is no column {col!r}")
        arr = table[col]
        if pa.types.is_timestamp(arr.type) and isinstance(value, str):
            value = pa.scalar(dt.datetime.fromisoformat(value), arr.type)
        fn = {"=": pc.equal, "<>": pc.not_equal, ">": pc.greater, ">=": pc.greater_equal, "<": pc.less, "<=": pc.less_equal}[op]
        try:
            return pc.fill_null(fn(arr, value), False)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as e:
            raise FilterError(f"cannot compare column {col!r} ({arr.type}) with {value!r}") from e
    if kind == "not":
        return pc.invert(eval_where(node[1], table))
    left, right = eval_where(node[1], table), eval_where(node[2], table)
    return pc.and_(left, right) if kind == "and" else pc.or_(left, right)


def scan(refs: list[FileRef], predicate, offset: int, length: int, budget: int = SCAN_BUDGET) -> dict:
    """Walk row groups in order, keep rows the predicate accepts, stop at ``offset + length`` matches or at the budget."""
    matches, seen_matches, scanned_rows, partial, requests, nbytes = [], 0, 0, False, 0, 0
    base = 0
    for ref in refs:
        s = parquet_summary(ref)
        f = RangeFile(ref.url, ref.size, budget=max(0, budget - nbytes))
        try:
            pf = pq.ParquetFile(f)       # opening reads the footer, which the budget can already forbid
        except BudgetExceeded:
            partial = True
            break
        row0 = base
        for i, g in enumerate(s["row_groups"]):
            try:
                table = pf.read_row_group(i)
            except BudgetExceeded:
                partial = True
                break
            mask = predicate(table)
            idx = np.flatnonzero(np.asarray(mask.to_numpy(zero_copy_only=False), dtype=bool))
            scanned_rows += table.num_rows
            for j in idx:
                if seen_matches >= offset and len(matches) < length:
                    matches.append((row0 + int(j), table.slice(int(j), 1)))
                seen_matches += 1
            row0 += table.num_rows
            if len(matches) >= length and seen_matches >= offset + length:
                partial = True   # stopped early: the total is unknown beyond what was scanned
                break
        requests += f.requests
        nbytes += f.bytes
        base += s["rows"]
        if partial:
            break
    out_rows = []
    for idx, t in matches:
        out_rows += table_rows(t, idx)
    return {"rows": out_rows, "matches_seen": seen_matches, "scanned_rows": scanned_rows, "partial": partial, "requests": requests, "bytes": nbytes}


def filter_rows(refs: list[FileRef], where: str, offset: int, length: int) -> dict:
    tree = parse_where(where)
    return scan(refs, lambda table: eval_where(tree, table), offset, max(1, min(length, MAX_ROWS)))


def search_rows(refs: list[FileRef], query: str, offset: int, length: int) -> dict:
    q = query

    def predicate(table: pa.Table):
        mask = None
        for name in table.column_names:
            if pa.types.is_string(table[name].type) or pa.types.is_large_string(table[name].type):
                m = pc.fill_null(pc.match_substring(table[name], q, ignore_case=True), False)
                mask = m if mask is None else pc.or_(mask, m)
        return mask if mask is not None else pa.array([False] * table.num_rows)

    return scan(refs, predicate, offset, max(1, min(length, MAX_ROWS)))


# ------------------------------------------------------------------------------------------------ statistics
def _kind(feature: dict, arrow_type: str) -> str:
    t = feature.get("_type")
    if t == "ClassLabel":
        return "class_label"
    if t in ("Image", "Audio"):
        return t.lower()
    if t == "List" or arrow_type.startswith("list"):
        return "list"
    dtype = feature.get("dtype", arrow_type)
    if dtype.startswith(("int", "uint")):
        return "int"
    if dtype.startswith(("float", "double", "decimal")):
        return "float"
    if dtype == "bool":
        return "bool"
    if dtype.startswith("timestamp") or dtype.startswith("date"):
        return "datetime"
    if dtype in ("string", "large_string"):
        return "string_text"
    return "other"


def statistics(refs: list[FileRef]) -> dict:
    """Exact count / min / max / nulls from footers; histograms from a bounded sample (and said so)."""
    summaries = [parquet_summary(r) for r in refs]
    columns = summaries[0]["columns"]
    total = sum(s["rows"] for s in summaries)
    out = []
    # a sample: first row groups until STATS_SAMPLE_ROWS
    sample_tables, sampled = [], 0
    for ref, s in zip(refs, summaries):
        picked = []
        for i, g in enumerate(s["row_groups"]):
            if sampled >= STATS_SAMPLE_ROWS:
                break
            picked.append(i); sampled += g["rows"]
        if picked:
            sample_tables.append(read_row_groups(ref, picked)[0])
        if sampled >= STATS_SAMPLE_ROWS:
            break
    sample = pa.concat_tables(sample_tables) if sample_tables else None
    for c in columns:
        name = c["name"]
        kind = _kind(c["feature"], c["type"])
        mins = [g["stats"].get(name, {}).get("min") for s in summaries for g in s["row_groups"]]
        maxs = [g["stats"].get(name, {}).get("max") for s in summaries for g in s["row_groups"]]
        nulls = [g["stats"].get(name, {}).get("nulls") for s in summaries for g in s["row_groups"]]
        have = all(v is not None for v in nulls) and all(v is not None for v in mins)
        info = {"null_count": sum(nulls) if have else None, "exact_from_footer": have}
        if have and kind in ("int", "float", "datetime", "string_text"):
            info["min"], info["max"] = min(mins), max(maxs)
        if sample is not None and name in sample.column_names:
            col = sample[name]
            if kind in ("int", "float"):
                vals = col.drop_null().to_numpy(zero_copy_only=False).astype(float)
                if len(vals):
                    hist, edges = np.histogram(vals, bins=10)
                    info.update(histogram={"hist": hist.tolist(), "bin_edges": [round(float(e), 4) for e in edges]}, mean=float(vals.mean()), median=float(np.median(vals)), std=float(vals.std()))
            elif kind == "class_label":
                names = c["feature"].get("names") or []
                counts = pc.value_counts(col.drop_null())
                info["frequencies"] = {(names[int(v["values"].as_py())] if names else str(v["values"].as_py())): v["counts"].as_py() for v in counts}
                info["n_unique"] = len(info["frequencies"])
            elif kind == "string_text":
                lens = pc.utf8_length(col.drop_null()).to_numpy(zero_copy_only=False).astype(float)
                if len(lens):
                    hist, edges = np.histogram(lens, bins=10)
                    info.update(lengths={"hist": hist.tolist(), "bin_edges": [round(float(e), 1) for e in edges], "mean": float(lens.mean())})
            elif kind == "bool":
                counts = pc.value_counts(col.drop_null())
                info["frequencies"] = {str(v["values"].as_py()).lower(): v["counts"].as_py() for v in counts}
        out.append({"column_name": name, "column_type": kind, "column_statistics": info})
    return {"num_examples": total, "statistics": out, "sampled_rows": sampled, "partial": sampled < total}


# ------------------------------------------------------------------------------------------------ tar archives
def _sample_key(name: str) -> tuple[str, str]:
    directory, _, base = name.rpartition("/")
    key, dot, ext = base.partition(".")
    return (f"{directory}/{key}" if directory else key), ext


def tar_index(ref: FileRef) -> dict:
    """Member offsets of a tar by walking its headers over Range reads (no member data is read), grouped into
    WebDataset samples (the members that share a key up to the first dot)."""

    def build():
        f = RangeFile(ref.url, ref.size)
        started = time.time()
        members, truncated = [], False
        with tarfile.open(fileobj=f, mode="r:") as tf:
            for info in tf:
                if not info.isfile():
                    continue
                members.append({"name": info.name, "offset": info.offset_data, "size": info.size})
                if len(members) >= TAR_MAX_MEMBERS or time.time() - started > TAR_TIME_LIMIT:
                    truncated = True
                    break
        samples: dict[str, list[dict]] = {}
        for m in members:
            key, ext = _sample_key(m["name"])
            samples.setdefault(key, []).append({**m, "ext": ext})
        return {"members": len(members), "samples": [{"key": k, "members": v} for k, v in samples.items()], "truncated": truncated,
                "requests": f.requests, "bytes": f.bytes, "file_size": ref.size}

    return cached(("tar-index", ref.sha), build)


# ------------------------------------------------------------------------------------------------ revision diff
def diff_tables(old_ref: FileRef, new_ref: FileRef, key: str | None, budget: int = 64 * 1024 * 1024) -> dict:
    """What changed between two revisions of one parquet table: schema, row count, and (with a key column) which rows."""
    so, sn = parquet_summary(old_ref), parquet_summary(new_ref)
    old_cols = {c["name"]: c["type"] for c in so["columns"]}
    new_cols = {c["name"]: c["type"] for c in sn["columns"]}
    schema = {
        "added": [c for c in new_cols if c not in old_cols],
        "removed": [c for c in old_cols if c not in new_cols],
        "type_changed": [{"column": c, "from": old_cols[c], "to": new_cols[c]} for c in new_cols if c in old_cols and old_cols[c] != new_cols[c]],
    }
    out = {"identical_content": old_ref.sha == new_ref.sha, "rows": {"old": so["rows"], "new": sn["rows"], "delta": sn["rows"] - so["rows"]},
           "schema": schema, "keyed": False}
    if old_ref.sha == new_ref.sha:
        return out
    key = key or next((c for c in ("id", "key", "uuid") if c in old_cols and c in new_cols), None)
    if key is None or key not in old_cols or key not in new_cols:
        return out
    to, fo = read_row_groups(old_ref, list(range(len(so["row_groups"]))), budget=budget)
    tn, fn = read_row_groups(new_ref, list(range(len(sn["row_groups"]))), budget=budget)
    old_df, new_df = to.to_pandas(), tn.to_pandas()
    merged = old_df.merge(new_df, on=key, how="outer", suffixes=("__old", "__new"), indicator=True)
    both = merged[merged["_merge"] == "both"]
    common = [c for c in old_cols if c in new_cols and c != key]
    changed_mask = np.zeros(len(both), dtype=bool)
    changed_cols: dict[str, int] = {}
    for c in common:
        diff = ~((both[f"{c}__old"] == both[f"{c}__new"]) | (both[f"{c}__old"].isna() & both[f"{c}__new"].isna())).to_numpy()
        changed_cols[c] = int(diff.sum())
        changed_mask |= diff
    sample_changed = both[changed_mask].head(5)
    out.update(
        keyed=True, key=key,
        added=int((merged["_merge"] == "right_only").sum()), removed=int((merged["_merge"] == "left_only").sum()),
        changed=int(changed_mask.sum()), changed_columns={k: v for k, v in changed_cols.items() if v},
        examples={
            "added": merged[merged["_merge"] == "right_only"][key].head(5).tolist(),
            "removed": merged[merged["_merge"] == "left_only"][key].head(5).tolist(),
            "changed": [{key: _stat_json(r[key]), **{c: {"old": _stat_json(r[f"{c}__old"]), "new": _stat_json(r[f"{c}__new"])} for c in common if changed_cols.get(c)}} for _, r in sample_changed.iterrows()],
        },
        requests=f"{fo.requests + fn.requests}", bytes=fo.bytes + fn.bytes,
    )
    return out
