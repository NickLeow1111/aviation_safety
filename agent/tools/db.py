"""Shared Synapse Dedicated SQL Pool connectivity.

Single source of truth for building an Entra-ID-authenticated pyodbc
connection (no secrets, no connection strings). Used by the NL2SQL tool
and the schema catalog.
"""
from __future__ import annotations

import os
import struct

import pyodbc
from azure.identity import DefaultAzureCredential

_credential = DefaultAzureCredential(
    exclude_interactive_browser_credential=False
)


def build_connection() -> pyodbc.Connection:
    """Open a Synapse connection using an Entra ID access token."""
    server = os.environ["SYNAPSE_SQL_SERVER"]  # e.g. workspace.sql.azuresynapse.net
    database = os.environ["SYNAPSE_SQL_DATABASE"]
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    token = _credential.get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    SQL_COPT_SS_ACCESS_TOKEN = 1256  # noqa: N806
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def query_timeout_secs() -> int:
    """Server-side query timeout (seconds). Caps runaway LLM-generated SQL."""
    raw = os.environ.get("SYNAPSE_QUERY_TIMEOUT_SECS", "45")
    try:
        return max(5, int(raw))
    except ValueError:
        return 45
