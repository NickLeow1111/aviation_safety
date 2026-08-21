"""NL→T-SQL tool for Synapse Dedicated SQL Pool.

Security:
- Auth via Microsoft Entra ID access token (DefaultAzureCredential).
- Connects to Synapse using ODBC Driver 18 with `Authentication=ActiveDirectoryAccessToken`.
- No username/password / no connection string secret.
- Statements are validated as a single read-only SELECT against vw_SafetyIntel_* views.

Reliability & trust layers:
- Live-schema validation: hallucinated view names fail fast with the list of
  available views; likely-bad columns produce warnings the model can act on.
- Server-side PII redaction: restricted contact/email columns are stripped
  from results regardless of what the SQL selects.
- Query timeout + retry/backoff on transient Azure SQL errors.
- Truncation is explicit: the model is told when the row cap cut data off.
"""
from __future__ import annotations

import logging
import os
import re
import time
from decimal import Decimal
from typing import Any

import pyodbc

from tools.db import build_connection, query_timeout_secs
from tools.schema_catalog import check_views_exist, find_column_issues

log = logging.getLogger(__name__)

# --- allowed surface --------------------------------------------------------
_ALLOWED_VIEW_PREFIX = "vw_safetyintel_"
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|truncate|grant|revoke|deny|exec|execute|"
    r"sp_|xp_|create|backup|restore|shutdown|kill|use|openrowset|opendatasource|bulk)\b",
    re.IGNORECASE,
)
_SELECT_RE = re.compile(r"^\s*(with\b.+?\bselect\b|select\b)", re.IGNORECASE | re.DOTALL)
_VIEW_REF_RE = re.compile(r"\b(?:dbo\.)?(vw_safetyintel_[a-z_]+)", re.IGNORECASE)

# Restricted columns per system prompt — never leave the server even if the
# LLM SELECTs them explicitly.
_PII_COLUMN_RE = re.compile(r"^(AM|QM|MM)_(Email|Contact)$", re.IGNORECASE)

# Transient SQLSTATEs worth retrying (throttling, transient faults, timeouts
# on connection establishment). See Azure SQL error codes docs.
_TRANSIENT_SQLSTATES = {"08S01", "HYT00", "HYT01", "08001", "08003", "40615", "40501", "40613"}
_MAX_RETRIES = 2


class SqlSafetyError(ValueError):
    """Raised when generated SQL violates the read-only allow-list."""


def _validate_sql(sql: str) -> None:
    sql_stripped = sql.strip().rstrip(";")
    if ";" in sql_stripped:
        raise SqlSafetyError("Multiple statements are not allowed.")
    if not _SELECT_RE.match(sql_stripped):
        raise SqlSafetyError("Only SELECT/CTE statements are allowed.")
    if _FORBIDDEN_RE.search(sql_stripped):
        raise SqlSafetyError("Statement contains forbidden keywords.")
    refs = _VIEW_REF_RE.findall(sql_stripped)
    if not refs:
        raise SqlSafetyError("Statement must reference vw_SafetyIntel_* views only.")
    for ref in refs:
        if not ref.lower().startswith(_ALLOWED_VIEW_PREFIX):
            raise SqlSafetyError(f"View '{ref}' is not in the allow-list.")


def _validate_against_live_schema(sql: str) -> list[str]:
    """Live-schema checks. Raises on unknown views; returns column warnings.

    Skipped silently when the schema catalog is unavailable.
    """
    referenced = [r for r in _VIEW_REF_RE.findall(sql)]
    missing, available = check_views_exist(referenced)
    if missing:
        raise SqlSafetyError(
            f"Unknown view(s): {', '.join(missing)}. "
            f"These do not exist in this deployment. Available views: "
            f"{', '.join(available)}. Use get_schema for columns/examples."
        )
    return find_column_issues(sql, referenced)


# --- public tool entry point ------------------------------------------------
def run_nl2sql(sql: str, max_rows: int = 200) -> dict[str, Any]:
    """Execute an LLM-generated SELECT against Synapse and return JSON rows.

    Args:
        sql: SQL produced by the LLM. Must be a single SELECT/CTE that reads
             only from vw_SafetyIntel_* views that actually exist.
        max_rows: hard cap on rows returned to the agent.

    Returns:
        {"columns": [...], "rows": [...], "row_count": n, "truncated": bool,
         "sql": <validated>, ["column_warnings": [...]]}
    """
    _validate_sql(sql)
    column_warnings = _validate_against_live_schema(sql)

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            raw_columns, fetched = _execute(sql, max_rows)
            break
        except (pyodbc.OperationalError, pyodbc.Error) as e:
            last_err = e
            if attempt >= _MAX_RETRIES or not _is_transient(e):
                raise
            delay = 2 ** attempt  # 1s, 2s
            log.warning(
                "transient SQL error (%s), retry %d/%d in %ss",
                getattr(e, "sqlstate", "?"), attempt + 1, _MAX_RETRIES, delay,
            )
            time.sleep(delay)

    assert raw_columns is not None  # for type checkers; set on success
    columns, rows, truncated = _redact(raw_columns, fetched, max_rows)

    result: dict[str, Any] = {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "sql": sql,
    }
    if truncated:
        result["note"] = (
            f"Result was truncated to the first {max_rows} rows. If you need "
            "complete counts, aggregate in SQL (COUNT/GROUP BY) instead of "
            "requesting raw rows."
        )
    if not rows:
        result["empty_result_hint"] = (
            "No rows returned. Before telling the user 'no data', double-check "
            "filters/date ranges/column names against get_schema."
        )
    if column_warnings:
        result["column_warnings"] = column_warnings
    return result


def _execute(sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
    """Run the query with a hard timeout; fetch max_rows+1 to detect truncation."""
    with build_connection() as conn:
        conn.timeout = query_timeout_secs()
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(max_rows + 1)
    return columns, fetched


def _redact(
    columns: list[str], fetched: list[tuple], max_rows: int
) -> tuple[list[str], list[dict[str, Any]], bool]:
    """Strip restricted PII columns; coerce values; detect truncation."""
    keep_idx = [
        i for i, c in enumerate(columns) if not _PII_COLUMN_RE.match(c or "")
    ]
    redacted_cols = [columns[i] for i in keep_idx]

    rows: list[dict[str, Any]] = []
    for r in fetched[:max_rows]:
        rows.append({columns[i]: _coerce(r[i]) for i in keep_idx})

    truncated = len(fetched) > max_rows
    if len(keep_idx) != len(columns):
        log.info("redacted %d restricted column(s) from nl2sql result",
                 len(columns) - len(keep_idx))
    return redacted_cols, rows, truncated


def _is_transient(err: Exception) -> bool:
    state = getattr(err, "sqlstate", None) or (
        err.args[0] if err.args and isinstance(err.args[0], str) else ""
    )
    return str(state).upper() in _TRANSIENT_SQLSTATES


def _coerce(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Decimal):
        if v == v.to_integral_value():
            return int(v)
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v
