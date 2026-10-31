"""Tests for the reassessment pass.

The rule this pins is the audit rule: a detection that turns out to be wrong is marked,
never deleted, and the mark is recomputed from stored positions rather than asserted. If
the pass ever starts invalidating something it cannot re-derive, these tests fail.
"""
from datetime import datetime, timedelta, timezone

import phase
import reclassify


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.owner.statements.append((" ".join(sql.split()), params))
        self.rowcount = 1

    def fetchone(self):
        return None

    def fetchall(self):
        return self.owner.events if "FROM safety_event" in self.owner.last_sql else self.owner.tracks


class FakeConn:
    def __init__(self, events=None, tracks=None):
        self.events = list(events or [])
        self.tracks = list(tracks or [])
        self.statements = []
        self.last_sql = ""

    def cursor(self):
        cursor = FakeCursor(self)
        original = cursor.execute

        def record(sql, params=None):
            self.last_sql = " ".join(sql.split())
            original(sql, params)

        cursor.execute = record
        return cursor


def sample(icao, altitude, vertical=0.0, speed=150.0, on_ground=False, offset=0.0):
    now = datetime.now(timezone.utc)
    return (icao, altitude, speed, vertical, on_ground, "CALL", (now + timedelta(seconds=offset)).timestamp())


def test_a_pair_where_one_aircraft_was_on_the_runway_is_invalid():
    when = datetime.now(timezone.utc)
    event = {"icao24": ["aaaaaa", "bbbbbb"]}
    tracks = {
        "aaaaaa": [
            {
                "icao24": "aaaaaa",
                "altitude_ft": 0,
                "ground_speed_kt": 147,
                "vertical_rate_fpm": 0,
                "on_ground": False,
                "t": when.timestamp(),
            }
        ],
        "bbbbbb": [
            {
                "icao24": "bbbbbb",
                "altitude_ft": 325,
                "ground_speed_kt": 148,
                "vertical_rate_fpm": -768,
                "on_ground": False,
                "t": when.timestamp(),
            }
        ],
    }
    verdict = reclassify._judge(event, tracks, when)
    assert verdict["verdict"] == "invalid"
    assert verdict["phases"]["aaaaaa"] == phase.GROUND
    assert "bbbbbb" not in verdict["reason"] or "ground traffic" in verdict["reason"]
    assert "aaaaaa" in verdict["reason"]


def test_two_aircraft_on_approach_survive():
    when = datetime.now(timezone.utc)
    event = {"icao24": ["aaaaaa", "bbbbbb"]}
    tracks = {
        "aaaaaa": [{"icao24": "aaaaaa", "altitude_ft": 875, "ground_speed_kt": 134, "vertical_rate_fpm": -577, "on_ground": False, "t": when.timestamp()}],
        "bbbbbb": [{"icao24": "bbbbbb", "altitude_ft": 1500, "ground_speed_kt": 140, "vertical_rate_fpm": -700, "on_ground": False, "t": when.timestamp()}],
    }
    assert reclassify._judge(event, tracks, when)["verdict"] == "valid"


def test_an_event_without_stored_positions_is_left_alone():
    """Absence of evidence is not evidence of a false positive."""
    verdict = reclassify._judge({"icao24": ["cccccc"]}, {}, datetime.now(timezone.utc))
    assert verdict["verdict"] == "unproven"
    assert verdict["reason"]


def test_the_phase_is_taken_at_the_event_time_not_from_anywhere_in_the_window():
    """An aircraft on the runway a minute before and climbing out a minute after is
    judged at the moment in question."""
    when = datetime.now(timezone.utc)
    tracks = {
        "aaaaaa": [
            {"icao24": "aaaaaa", "altitude_ft": 0, "ground_speed_kt": 20, "vertical_rate_fpm": 0, "on_ground": True, "t": when.timestamp() - 50},
            {"icao24": "aaaaaa", "altitude_ft": 2400, "ground_speed_kt": 240, "vertical_rate_fpm": 2600, "on_ground": False, "t": when.timestamp() + 40},
        ],
        "bbbbbb": [
            {"icao24": "bbbbbb", "altitude_ft": 3000, "ground_speed_kt": 250, "vertical_rate_fpm": 0, "on_ground": False, "t": when.timestamp()},
        ],
    }
    assert reclassify._judge({"icao24": ["aaaaaa", "bbbbbb"]}, tracks, when)["verdict"] == "valid"


def test_applying_marks_the_row_and_keeps_it():
    when = datetime.now(timezone.utc)
    conn = FakeConn()
    findings = [{"id": 9, "reason": "ground traffic", "phases": {"aaaaaa": "ground"}}]
    written = reclassify._invalidate(conn, findings)

    assert written == 1
    sql, params = conn.statements[0]
    assert sql.startswith("UPDATE safety_event SET invalidated_at = now()")
    # Nothing may remove the row.
    assert "DELETE" not in sql.upper()
    assert params[0] == "ground traffic"
    assert params[1] == reclassify.INVALIDATED_BY
    assert params[3] == 9


def test_the_dry_run_writes_nothing():
    when = datetime.now(timezone.utc)
    event_row = (
        9,
        when,
        "loss_of_separation",
        ["AAAA", "BBBB"],
        ["aaaaaa", "bbbbbb"],
        "WSSS",
        "0.93",
        "225",
        "snapshot_separation",
    )
    conn = FakeConn(events=[event_row], tracks=[])
    result = reclassify.reassess(conn, apply=False)

    assert result["checked"] == 1
    assert result["unproven"] == 1
    assert result["applied"] is False
    assert not any(statement[0].startswith("UPDATE") for statement in conn.statements)


def test_an_invalidation_carries_its_own_evidence():
    """The audit trail is the point: a later reader must see why, from whose hand, when."""
    conn = FakeConn()
    reclassify._invalidate(conn, [{"id": 4, "reason": "ground traffic", "phases": {"aaaaaa": "ground"}}])
    sql, params = conn.statements[0]
    assert "metrics = COALESCE(metrics, '{}'::jsonb) || %s::jsonb" in sql
    assert '"revalidation"' in params[2]
    assert reclassify.INVALIDATED_BY in params[2]
