"""Unit tests for tools.nl2sql — validators, redaction, truncation, retries.

No database required: everything here exercises pure logic.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from tools import nl2sql
from tools.nl2sql import (
    SqlSafetyError,
    _is_transient,
    _redact,
    _validate_sql,
    run_nl2sql,
)


# ---------------------------------------------------------------------------
# Static SQL validation (allow-list enforcement)
# ---------------------------------------------------------------------------

class TestValidateSql:
    def test_plain_select_ok(self):
        _validate_sql("SELECT TOP 5 * FROM dbo.vw_SafetyIntel_AMO")

    def test_cte_select_ok(self):
        _validate_sql(
            "WITH t AS (SELECT AWI FROM dbo.vw_SafetyIntel_AMO) "
            "SELECT AWI FROM t"
        )

    def test_case_insensitive_view_prefix(self):
        _validate_sql("select * from vw_safetyintel_tamtam")

    def test_rejects_update(self):
        with pytest.raises(SqlSafetyError):
            _validate_sql("UPDATE dbo.vw_SafetyIntel_AMO SET Tier = 1")

    def test_rejects_insert(self):
        with pytest.raises(SqlSafetyError):
            _validate_sql("INSERT INTO dbo.vw_SafetyIntel_AMO VALUES (1)")

    def test_rejects_multiple_statements(self):
        with pytest.raises(SqlSafetyError, match="Multiple statements"):
            _validate_sql(
                "SELECT 1 FROM dbo.vw_SafetyIntel_AMO; SELECT 2 FROM dbo.vw_SafetyIntel_TAM"
            )

    def test_rejects_non_view_table(self):
        with pytest.raises(SqlSafetyError, match="vw_SafetyIntel"):
            _validate_sql("SELECT * FROM dbo.DM_TBL_SRG_AMO_TIER")

    def test_rejects_exec(self):
        # Non-SELECT shape is caught by the statement-type rule...
        with pytest.raises(SqlSafetyError):
            _validate_sql("EXEC dbo.some_proc")
        # ...and embedded forbidden functions are caught by the blocklist.
        with pytest.raises(SqlSafetyError, match="forbidden"):
            _validate_sql(
                "SELECT * FROM dbo.vw_SafetyIntel_AMO "
                "UNION ALL SELECT * FROM OPENROWSET('x')"
            )

    def test_trailing_semicolon_allowed(self):
        # rstrip(";") handles the common LLM habit of a trailing semicolon.
        _validate_sql("SELECT TOP 1 * FROM dbo.vw_SafetyIntel_TAM;")

    def test_created_date_column_not_flagged_as_forbidden(self):
        # \b(create)\b must not match inside Created_Date.
        _validate_sql(
            "SELECT Created_Date FROM dbo.vw_SafetyIntel_Audits WHERE Created_Date > '2026-01-01'"
        )


# ---------------------------------------------------------------------------
# PII redaction + truncation + coercion
# ---------------------------------------------------------------------------

class TestRedact:
    def test_pii_columns_stripped(self):
        cols = ["AWI", "AM_Email", "QM_Contact", "MM_Email", "Tier"]
        fetched = [("AWI/004", "a@x.com", "999", "b@y.com", 2)]
        out_cols, rows, truncated = _redact(cols, fetched, max_rows=200)
        assert out_cols == ["AWI", "Tier"]
        assert rows == [{"AWI": "AWI/004", "Tier": 2}]
        assert truncated is False

    def test_normal_contact_columns_kept(self):
        # Only AM/QM/MM person contacts are restricted — org-level fields stay.
        cols = ["Organisation_Contact"]
        _, rows, _ = _redact(cols, [("123",)], max_rows=10)
        assert rows == [{"Organisation_Contact": "123"}]

    def test_truncation_flag_set_when_extra_row_fetched(self):
        cols = ["AWI"]
        fetched = [("a",), ("b",), ("c",)]  # asked for 2 → got 3
        _, rows, truncated = _redact(cols, fetched, max_rows=2)
        assert len(rows) == 2
        assert truncated is True

    def test_decimal_and_datetime_coercion(self):
        cols = ["Count", "Price", "Updated"]
        fetched = [(Decimal("3.00"), Decimal("3.75"), datetime(2026, 8, 4, 9, 30))]
        _, rows, _ = _redact(cols, fetched, max_rows=10)
        assert rows[0]["Count"] == 3
        assert rows[0]["Price"] == 3.75
        assert rows[0]["Updated"] == "2026-08-04T09:30:00"


# ---------------------------------------------------------------------------
# Transient error classification
# ---------------------------------------------------------------------------

class FakeSqlErr(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class TestTransient:
    @pytest.mark.parametrize("state", ["40615", "40501", "08S01", "HYT00"])
    def test_transient_states_match(self, state):
        assert _is_transient(FakeSqlErr(state)) is True

    @pytest.mark.parametrize("state", ["42S02", "23000", "S0001"])
    def test_permanent_states_do_not_retry(self, state):
        assert _is_transient(FakeSqlErr(state)) is False


# ---------------------------------------------------------------------------
# run_nl2sql orchestration (DB mocked at _execute boundary)
# ---------------------------------------------------------------------------

class TestRunNl2sql:
    def test_unknown_view_fails_fast_with_available_list(self, monkeypatch):
        monkeypatch.setattr(
            nl2sql, "_validate_against_live_schema",
            lambda sql: (_ for _ in ()).throw(
                SqlSafetyError(
                    "Unknown view(s): vw_SafetyIntel_Bogus. Available views: "
                    "dbo.vw_SafetyIntel_AMO. Use get_schema."
                )
            ),
        )
        with pytest.raises(SqlSafetyError, match="get_schema"):
            run_nl2sql("SELECT * FROM dbo.vw_SafetyIntel_Bogus")

    def test_column_warnings_surface_in_result(self, monkeypatch):
        monkeypatch.setattr(
            nl2sql, "_validate_against_live_schema",
            lambda sql: ["Column 'Bogus_Col' does not exist in the queried view(s)."],
        )
        monkeypatch.setattr(
            nl2sql, "_execute",
            lambda sql, max_rows: (["AWI"], [("AWI/001",)]),
        )
        result = run_nl2sql("SELECT AWI, Bogus_Col FROM dbo.vw_SafetyIntel_AMO")
        assert result["column_warnings"]
        assert result["row_count"] == 1

    def test_empty_result_includes_hint(self, monkeypatch):
        monkeypatch.setattr(nl2sql, "_validate_against_live_schema", lambda sql: [])
        monkeypatch.setattr(nl2sql, "_execute", lambda sql, mr: (["AWI"], []))
        result = run_nl2sql("SELECT AWI FROM dbo.vw_SafetyIntel_AMO")
        assert result["rows"] == []
        assert "empty_result_hint" in result

    def test_note_present_when_truncated(self, monkeypatch):
        monkeypatch.setattr(nl2sql, "_validate_against_live_schema", lambda sql: [])
        monkeypatch.setattr(
            nl2sql, "_execute",
            lambda sql, mr: (["AWI"], [("a",), ("b",), ("c",)]),
        )
        result = run_nl2sql("SELECT AWI FROM dbo.vw_SafetyIntel_AMO", max_rows=2)
        assert result["truncated"] is True
        assert "aggregate in SQL" in result["note"]
