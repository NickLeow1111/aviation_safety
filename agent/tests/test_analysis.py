"""Tests for the analysis surface.

The three endpoints here all make a claim the operations board cannot, and each claim has a
failure mode worth pinning:

- a case file that hides bad tracks is worse than no case file, so the quality block is
  asserted, including when the answer is "the evidence was sound";
- a rate without a denominator is a count wearing a percentage, so the aircraft field must
  survive even when the traffic query returns nothing;
- a "recurring pair" built from two empty callsign lists is not a pair.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import api_analysis


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.owner.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.owner.one.pop(0) if self.owner.one else (0,)

    def fetchall(self):
        return self.owner.many.pop(0) if self.owner.many else []


class FakeConn:
    def __init__(self, one=None, many=None):
        self.statements = []
        self.one = list(one or [])
        self.many = list(many or [])

    def cursor(self):
        return FakeCursor(self)


class _Context:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def _event_row(**overrides):
    row = {
        "id": 2393,
        "event_time": datetime(2026, 11, 2, 2, 27, 49, tzinfo=timezone.utc),
        "event_type": "loss_of_separation",
        "severity": "high",
        "lat": 2.66,
        "lon": 101.77,
        "place_icao": "WMKK",
        "place_name": "Kuala Lumpur International",
        "altitude_ft": 1800.0,
        "callsigns": ["MAS709", "MXD2105"],
        "icao24": ["7502d8", "75058c"],
        "registration": None,
        "metrics": {"separation_nm": 0.02, "vertical_ft": 0.0},
        "source": "derived",
        "provenance": "detector:snapshot_separation",
        "external_ref": None,
        "ingested_at": datetime(2026, 11, 2, 2, 28, tzinfo=timezone.utc),
        "detector": "snapshot_separation",
        "invalidated_at": None,
        "invalidation_reason": None,
        "raw": None,
    }
    row.update(overrides)
    return tuple(row.values())


def test_case_file_reports_the_evidence_quality(monkeypatch):
    """A detection is a claim; the case file has to return what tests it."""
    conn = FakeConn(
        one=[
            _event_row(),
            (2, 9, 5, 1),  # 2 aircraft, 9 steps, 5 repeated fixes, 1 implausible segment
        ],
        many=[
            [(2377, datetime(2026, 11, 2, 1, 10, tzinfo=timezone.utc), "loss_of_separation", "high", "WMKK", ["MAS709", "MXD2105"], 4629)],
        ],
    )
    monkeypatch.setattr(api_analysis.store, "connect", lambda: _Context(conn))

    body = api_analysis.case_file(2393)

    assert body["event"]["callsigns"] == ["MAS709", "MXD2105"]
    assert body["quality"] == {"aircraft": 2, "steps": 9, "repeated_fixes": 5, "implausible_segments": 1}
    assert body["related"][0]["id"] == 2377
    assert body["related"][0]["delta_s"] == 4629.0


def test_case_file_without_aircraft_does_not_invent_quality(monkeypatch):
    """An event naming no aircraft cannot have its tracks judged — say nothing, not zero."""
    conn = FakeConn(one=[_event_row(icao24=[], callsigns=[])], many=[[]])
    monkeypatch.setattr(api_analysis.store, "connect", lambda: _Context(conn))

    body = api_analysis.case_file(1)

    assert body["quality"]["steps"] == 0
    assert body["quality"]["aircraft"] == 0
    # The track query must not have been issued at all.
    assert all("aircraft_track" not in sql for sql, _ in conn.statements)


def test_case_file_of_a_missing_event_is_a_404(monkeypatch):
    conn = FakeConn(one=[None])
    monkeypatch.setattr(api_analysis.store, "connect", lambda: _Context(conn))

    with pytest.raises(Exception) as caught:
        api_analysis.case_file(999)
    assert getattr(caught.value, "status_code", None) == 404


def test_rates_keep_the_denominator(monkeypatch):
    """Events per aircraft, or the number says nothing about risk."""
    conn = FakeConn(
        one=[(datetime(2026, 10, 30, tzinfo=timezone.utc), datetime(2026, 11, 2, tzinfo=timezone.utc), 358)],
        many=[
            [("WSSS", 162, 40)],   # events and high-severity count, per place
            [("WSSS", 953)],       # distinct aircraft within 10 NM, counted in one pass
            [],                    # events by hour
            [],                    # aircraft by hour
            [],                    # events by type
        ],
    )
    monkeypatch.setattr(api_analysis.store, "connect", lambda: _Context(conn))
    api_analysis._CACHE.clear()  # the payload is cached by window; a test must not read an earlier one

    body = api_analysis.rates(days=7)

    place = next(item for item in body["places"] if item["place_icao"] == "WSSS")
    assert place["events"] == 162
    assert place["aircraft"] == 953
    assert place["per_100_aircraft"] == pytest.approx(17.0, abs=0.01)
    assert "not a certified count" in body["note"]
    assert body["computed_at"]


def test_rates_are_cached_between_calls(monkeypatch):
    """The denominators cost a full scan on a single core; a second tab must not pay again."""
    conn = FakeConn(
        one=[(None, None, 0)],
        many=[[("WSSS", 1, 0)], [("WSSS", 10)], [], [], []],
    )
    monkeypatch.setattr(api_analysis.store, "connect", lambda: _Context(conn))
    api_analysis._CACHE.clear()

    first = api_analysis.rates(days=7)
    statements_after_first = len(conn.statements)
    second = api_analysis.rates(days=7)

    assert second is first
    assert len(conn.statements) == statements_after_first


def test_recurrence_ignores_an_empty_callsign_pair(monkeypatch):
    """Two aircraft with no callsign are not a recurring pair."""
    conn = FakeConn(one=[], many=[[], []])
    monkeypatch.setattr(api_analysis.store, "connect", lambda: _Context(conn))

    api_analysis.recurrence(days=30, min_events=3)

    pair_sql = conn.statements[0][0]
    assert "array_length(callsigns, 1) > 0" in pair_sql
    assert "HAVING COUNT(*) >= %s" in pair_sql
