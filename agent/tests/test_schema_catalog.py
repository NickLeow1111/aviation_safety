"""Unit tests for tools.schema_catalog — validation heuristics + formatting.

No database required: get_schemas is monkeypatched with fake catalogs.
"""
import pytest

from tools import schema_catalog as sc


def _fake_schemas():
    return {
        "vw_SafetyIntel_AMO": {
            "columns": [
                {"name": "AWI", "type": "varchar"},
                {"name": "Organisation_Name", "type": "varchar"},
                {"name": "Current_Tier", "type": "int"},
            ],
            "sample_rows": [{"AWI": "AWI/004", "Organisation_Name": "Acme", "Current_Tier": 2}],
        },
        "vw_SafetyIntel_Occurrences": {
            "columns": [
                {"name": "Occurrence_ID", "type": "int"},
                {"name": "Activity_Code", "type": "varchar"},
                {"name": "Occurrence_Date", "type": "date"},
            ],
            "sample_rows": [],
        },
    }


@pytest.fixture(autouse=True)
def patched_catalog(monkeypatch):
    monkeypatch.setattr(sc, "get_schemas", _fake_schemas)


# ---------------------------------------------------------------------------
# View existence
# ---------------------------------------------------------------------------

class TestCheckViewsExist:
    def test_known_view_not_missing(self):
        missing, available = sc.check_views_exist(["vw_SafetyIntel_AMO"])
        assert missing == []
        assert "vw_SafetyIntel_AMO" in available

    def test_hallucinated_view_flagged(self):
        missing, available = sc.check_views_exist(["vw_SafetyIntel_BirdStrikez"])
        assert missing == ["vw_SafetyIntel_BirdStrikez"]
        assert len(available) == 2

    def test_case_insensitive(self):
        missing, _ = sc.check_views_exist(["VW_SAFETYINTEL_AMO"])
        assert missing == []


# ---------------------------------------------------------------------------
# Column heuristics
# ---------------------------------------------------------------------------

class TestFindColumnIssues:
    def test_valid_columns_clean(self):
        sql = (
            "SELECT AWI, Organisation_Name FROM dbo.vw_SafetyIntel_AMO "
            "WHERE Current_Tier = 2"
        )
        assert sc.find_column_issues(sql, ["vw_SafetyIntel_AMO"]) == []

    def test_hallucinated_column_flagged(self):
        sql = "SELECT AWI, Tier_Name FROM dbo.vw_SafetyIntel_AMO"
        issues = sc.find_column_issues(sql, ["vw_SafetyIntel_AMO"])
        assert len(issues) == 1
        assert "tier_name" in issues[0].lower()

    def test_underscoreless_tokens_ignored(self):
        # Tokens without underscores are never flagged (too noisy).
        sql = "SELECT AWI FROM dbo.vw_SafetyIntel_AMO WHERE Organisation = 'x'"
        assert sc.find_column_issues(sql, ["vw_SafetyIntel_AMO"]) == []

    def test_string_literal_contents_not_tokenized(self):
        sql = (
            "SELECT Occurrence_ID FROM dbo.vw_SafetyIntel_Occurrences "
            "WHERE Activity_Code = 'Not_A_Real_Code'"
        )
        assert sc.find_column_issues(sql, ["vw_SafetyIntel_Occurrences"]) == []

    def test_multi_view_union_of_columns(self):
        # A column from EITHER referenced view is acceptable.
        sql = (
            "SELECT a.AWI, o.Occurrence_ID FROM dbo.vw_SafetyIntel_AMO a "
            "JOIN dbo.vw_SafetyIntel_Occurrences o ON 1=1"
        )
        assert sc.find_column_issues(sql, ["vw_SafetyIntel_AMO", "vw_SafetyIntel_Occurrences"]) == []

    def test_no_catalog_means_no_issues(self, monkeypatch):
        monkeypatch.setattr(sc, "get_schemas", lambda: None)
        sql = "SELECT Totally_Bogus_Col FROM dbo.vw_SafetyIntel_AMO"
        assert sc.find_column_issues(sql, ["vw_SafetyIntel_AMO"]) == []


# ---------------------------------------------------------------------------
# Formatting for prompt / tool payload
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_format_schema_reference_lists_views_and_columns(self):
        text = sc.format_schema_reference()
        assert text and "## Live view schemas" in text
        assert "**dbo.vw_SafetyIntel_AMO**" in text
        assert "Organisation_Name [varchar]" in text

    def test_format_scope_section_classifies_domains(self):
        text = sc.format_scope_section()
        assert "`vw_SafetyIntel_AMO`" in text
        assert "occurrence" in text  # domain classification picked up Occurrences view

    def test_get_schema_payload_all_views(self):
        payload = sc.get_schema_payload(None)
        assert payload["view_count"] == 2
        names = {v["name"] for v in payload["views"]}
        assert "dbo.vw_SafetyIntel_AMO" in names

    def test_get_schema_payload_filtered(self):
        payload = sc.get_schema_payload("occurrence")
        assert payload["view_count"] == 1
        assert payload["views"][0]["name"] == "dbo.vw_SafetyIntel_Occurrences"

    def test_get_schema_payload_example_rows_included(self):
        payload = sc.get_schema_payload("amo")
        view = payload["views"][0]
        assert view["example_rows"][0]["AWI"] == "AWI/004"

    def test_get_schema_payload_unavailable_returns_error(self, monkeypatch):
        monkeypatch.setattr(sc, "get_schemas", lambda: None)
        payload = sc.get_schema_payload(None)
        assert "error" in payload
