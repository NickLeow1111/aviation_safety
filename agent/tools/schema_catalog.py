"""Live schema catalog for vw_SafetyIntel_* views.

Gives the agent ground truth about what views/columns actually exist in
Synapse instead of relying on few-shot examples alone. Three consumers:

1. `get_schema` tool  — the LLM can look up view columns + sample rows.
2. nl2sql validation  — fail fast on hallucinated view names; warn on
                        likely-bad column references before execution.
3. Agent creation     — inject a live schema reference into the system
                        prompt (see scripts/create_foundry_agent.py).

All DB access is best-effort and cached: if Synapse is unreachable the
callers degrade gracefully (validation skipped, tool returns an error).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

from tools.db import build_connection

log = logging.getLogger(__name__)

_VIEW_PREFIX = "vw_safetyintel_"
_TTL_SECS = int(os.environ.get("SCHEMA_CACHE_TTL_SECS", "3600"))
_MAX_SAMPLE_ROWS = 2

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "schemas": None}


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def _introspect() -> dict[str, dict[str, Any]]:
    """Query INFORMATION_SCHEMA + TOP-N samples for every SafetyIntel view."""
    schemas: dict[str, dict[str, Any]] = {}
    with build_connection() as conn:
        conn.timeout = 30
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME LIKE 'vw_SafetyIntel%'
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """
            )
            for tbl, col, dtype in cur.fetchall():
                entry = schemas.setdefault(
                    tbl,
                    {"columns": [], "sample_rows": []},
                )
                entry["columns"].append({"name": col, "type": dtype})

    # Sample rows are extremely useful to the LLM (value formats, code
    # vocabularies like Activity_Code / Finding_Level) but must never break
    # cataloging — skip any individual view that fails.
    for view_name, entry in schemas.items():
        try:
            with build_connection() as conn2:
                conn2.timeout = 30
                with conn2.cursor() as cur2:
                    cur2.execute(
                        f"SELECT TOP {_MAX_SAMPLE_ROWS} * FROM dbo.[{view_name}]"
                    )
                    cols = [d[0] for d in cur2.description or []]
                    raw = cur2.fetchall()
            entry["sample_rows"] = [
                {c: _jsonable(v) for c, v in zip(cols, row)} for row in raw
            ]
        except Exception:  # noqa: BLE001
            log.warning("sample rows unavailable for %s", view_name, exc_info=True)

    return schemas


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def get_schemas(force: bool = False) -> dict[str, dict[str, Any]] | None:
    """Return {view_name: {columns, sample_rows}}, or None when unavailable.

    Cached for SCHEMA_CACHE_TTL_SECS (default 1h). Never raises — callers
    treat None as "validation unavailable".
    """
    now = time.monotonic()
    with _cache_lock:
        if (
            not force
            and _cache["schemas"] is not None
            and now - _cache["ts"] < _TTL_SECS
        ):
            return _cache["schemas"]

    try:
        schemas = _introspect()
    except Exception as e:  # noqa: BLE001
        log.warning("schema introspection failed: %s", e)
        return None if _cache["schemas"] is None else _cache["schemas"]

    with _cache_lock:
        _cache["ts"] = time.monotonic()
        _cache["schemas"] = schemas
    log.info("schema catalog refreshed: %d views", len(schemas))
    return schemas


# ---------------------------------------------------------------------------
# get_schema tool payload
# ---------------------------------------------------------------------------

def get_schema_payload(view_filter: str | None = None) -> dict[str, Any]:
    """Compact JSON payload for the LLM-facing `get_schema` tool."""
    schemas = get_schemas()
    if schemas is None:
        return {
            "error": "Schema catalog unavailable (cannot reach Synapse right now).",
        }

    flt = (view_filter or "").strip().lower().lstrip("dbo.").replace("dbo.", "")
    names = sorted(schemas)
    if flt:
        names = [n for n in names if flt.replace("vw_safetyintel_", "") in n.lower() or flt in n.lower()]

    views = []
    for name in names:
        entry = schemas[name]
        views.append({
            "name": f"dbo.{name}",
            "columns": [f"{c['name']} ({c['type']})" for c in entry["columns"]],
            "example_rows": entry.get("sample_rows") or [],
        })
    return {"view_count": len(views), "views": views}


# ---------------------------------------------------------------------------
# Validation helpers used by run_nl2sql
# ---------------------------------------------------------------------------

def check_views_exist(referenced: list[str]) -> tuple[list[str], list[str]]:
    """Split referenced views into (missing, available).

    Missing = not present in the live database (likely hallucinated).
    """
    schemas = get_schemas()
    if schemas is None:
        return [], []
    live = {name.lower() for name in schemas}
    missing = [v for v in referenced if v.lower() not in live]
    available = sorted(schemas)
    return missing, available


_COLUMN_TOKEN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b")

# Tokens that are never column names even when they contain underscores.
_NON_COLUMN_TOKENS = {
    "safetyintel", "select", "from", "where", "group_by", "order_by",
    "inner_join", "left_join", "outer_join", "cross_join", "full_join",
}

_SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "in", "as", "on", "join",
    "inner", "left", "right", "full", "outer", "cross", "group", "by",
    "order", "having", "top", "distinct", "case", "when", "then", "else",
    "end", "is", "null", "like", "between", "with", "union", "all", "asc",
    "desc", "over", "partition", "cast", "convert", "try_cast", "coalesce",
    "isnull", "nullif", "iif", "len", "sum", "count", "avg", "min", "max",
    "year", "month", "day", "dateadd", "datediff", "datepart", "getdate",
    "format", "concat", "upper", "lower", "ltrim", "rtrim", "trim",
    "substring", "replace", "round", "floor", "ceiling", "abs", "stdev",
    "var", "row_number", "rank", "dense_rank", "ntile", "lag", "lead",
    "first_value", "last_value", "string_agg", "dbo", "database", "schema",
    "tables", "columns", "values", "set", "exists", "any", "some", "escape",
    "offset", "rows", "range", "current", "following", "unbounded",
    "preceding", "percent", "ties", "xml", "path", "type", "into",
}


def find_column_issues(
    sql: str,
    referenced_views: list[str],
) -> list[str]:
    """Best-effort detection of likely-hallucinated column references.

    Heuristic, deliberately conservative: only flags underscore-containing
    tokens (the naming style of this warehouse) that appear in no referenced
    view's column list. Returns human-readable warnings; empty list = clean.
    Warn-only by design — never blocks execution (aliases / expressions can
    legitimately produce unknown-looking tokens).
    """
    schemas = get_schemas()
    if schemas is None or not referenced_views:
        return []

    known_columns: set[str] = set()
    for view in referenced_views:
        entry = schemas.get(view) or next(
            (s for n, s in schemas.items() if n.lower() == view.lower()),
            None,
        )
        if entry:
            known_columns.update(c["name"].lower() for c in entry["columns"])

    if not known_columns:
        return []

    # Strip string literals so their contents are not tokenized.
    stripped = re.sub(r"'(?:[^']|'')*'", "''", sql)
    candidates = [
        t.lower() for t in _COLUMN_TOKEN_RE.findall(stripped)
        if "_" in t
        and t.lower() not in _SQL_KEYWORDS
        and t.lower() not in _NON_COLUMN_TOKENS
        and not t.startswith("vw_")
        and not t.startswith("@")
    ]

    issues: list[str] = []
    seen: set[str] = set()
    for token in sorted(set(candidates)):
        if token in seen:
            continue
        seen.add(token)
        if token not in known_columns:
            issues.append(token)
    return [
        f"Column '{tok}' does not exist in the queried view(s). "
        "Check spelling against the view schema (use get_schema)."
        for tok in issues[:5]
    ]


# ---------------------------------------------------------------------------
# Prompt injection text (used at agent-creation time)
# ---------------------------------------------------------------------------

def format_schema_reference(max_views: int = 40) -> str | None:
    """Markdown block listing every view + its columns, for the system prompt.

    Returns None when the catalog is unavailable (caller keeps static text).
    """
    schemas = get_schemas()
    if schemas is None:
        return None

    lines: list[str] = ["## Live view schemas (auto-generated)", ""]
    for name in sorted(schemas)[:max_views]:
        entry = schemas[name]
        cols = ", ".join(f"{c['name']} [{c['type']}]" for c in entry["columns"])
        lines.append(f"- **dbo.{name}**: {cols}")
    lines.append("")
    return "\n".join(lines)


def format_scope_section() -> str | None:
    """Deployment-scope section generated from what is ACTUALLY deployed."""
    schemas = get_schemas()
    if schemas is None:
        return None

    names = sorted(schemas)
    bullets = "\n".join(f"- `{n}`" for n in names)
    groups = {
        "occurrence": "occurrences, occurrence-ops tracks/overview, hotspots",
        "audit": "audits, surveillance, tactical audit",
        "amo": "AMO registry, tier history, TAM arrangements",
        "aoc": "AOC registry",
    }
    covered = [
        label for key, label in groups.items()
        if any(key in name.lower() for name in names)
    ]
    coverage_note = (
        "Loaded data domains: " + "; ".join(covered) + "."
        if covered else "Loaded domains could not be classified automatically."
    )
    return "\n".join([
        "## Deployment data scope (live)",
        "",
        "The following views exist in this deployment and may be queried:",
        "",
        bullets,
        "",
        coverage_note,
        "If a user asks about a domain whose views are NOT listed above, say",
        "that dataset is not available here. Do not guess view names.",
        "",
    ])
