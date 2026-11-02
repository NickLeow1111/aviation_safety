"""Tests for the one-flight profile.

Two behaviours matter more than the happy path:

- an aircraft with no fixes must say so, not return an empty graph that looks like a flat line;
- the profile must describe what was *observed* — fix count, span, coverage — so a gap in the
  feed cannot be mistaken for an aircraft holding altitude.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import api_aircraft


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list[str] = []
        self.params: list[tuple] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(tuple(params or ()))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch, rows):
    conn = FakeConn(rows)
    monkeypatch.setattr(api_aircraft.store, "connect", lambda: conn)
    return conn


def _lookup_row(minutes_ago: float, altitude: float, speed: float) -> tuple:
    """Matches the lookup SELECT: icao24 first, observed_at last."""
    return (
        "76b865",
        "SIA122",
        1.35,
        103.99,
        altitude,
        speed,
        90.0,
        1200.0,
        False,
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def _fix(minutes_ago: float, altitude: float, speed: float) -> tuple:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        1.35,
        103.99,
        altitude,
        speed,
        90.0,
        1200.0,
        False,
        "SIA122",
        "A359",
    )


def test_profile_reports_what_was_observed(monkeypatch):
    """Newest-first rows come back oldest-first, with the span and coverage stated."""
    rows = [_fix(1, 3000, 210), _fix(2, 2000, 190), _fix(3, 1000, 170)]  # DESC order from SQL
    _patch(monkeypatch, rows)

    body = api_aircraft.profile("76b865", minutes=20)

    assert body["callsign"] == "SIA122"
    assert [sample["altitude_ft"] for sample in body["samples"]] == [1000, 2000, 3000]
    assert body["summary"]["fixes"] == 3
    assert body["summary"]["altitude_min_ft"] == 1000
    assert body["summary"]["altitude_max_ft"] == 3000
    assert body["summary"]["aircraft_type"] == "A359"
    assert 0 < body["summary"]["coverage"] <= 1
    assert body["note"] is None


def test_profile_without_fixes_says_so(monkeypatch):
    """An empty profile must not render as a flat line at zero."""
    _patch(monkeypatch, [])

    body = api_aircraft.profile("abcdef", minutes=20)

    assert body["samples"] == []
    assert body["summary"] is None
    assert "No fixes" in body["note"]


def test_profile_survives_fixes_without_altitude(monkeypatch):
    """Grounded aircraft report no altitude; the summary must not crash on it."""
    rows = [_fix(1, None, 12), _fix(2, 900, 180)]
    _patch(monkeypatch, rows)

    body = api_aircraft.profile("76b865", minutes=20)

    assert body["summary"]["fixes"] == 2
    assert body["summary"]["altitude_min_ft"] == 900
    assert body["summary"]["altitude_max_ft"] == 900


def test_profile_rejects_an_empty_icao24(monkeypatch):
    _patch(monkeypatch, [])
    with pytest.raises(api_aircraft.HTTPException):
        api_aircraft.profile("   ", minutes=20)


def test_lookup_finds_a_flight_outside_the_viewport(monkeypatch):
    """The map's live list is viewport-scoped; a callsign lookup must not be."""
    row = _lookup_row(2, 12000, 280)
    conn = _patch(monkeypatch, [row])

    body = api_aircraft.lookup("sia122", minutes=15)

    assert body["found"] is True
    assert body["callsign"] == "SIA122"
    assert body["fix"]["icao24"] == row[0]
    assert body["fix"]["altitude_ft"] == 12000
    assert body["fix"]["age_s"] is not None
    assert body["fix"]["observed_at"].endswith("+00:00")
    assert "upper(trim(callsign))" in conn.cursor_obj.statements[0]


def test_lookup_reports_a_miss(monkeypatch):
    _patch(monkeypatch, [])

    body = api_aircraft.lookup("NOSUCH1", minutes=15)

    assert body["found"] is False
    assert body["fix"] is None
    assert "No fix" in body["note"]
