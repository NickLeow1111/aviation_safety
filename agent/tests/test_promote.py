"""Tests for promoting detections into the event store.

The risk this guards against is not a crash — it is a record that looks authoritative
and is wrong. So the tests pin: a projection never becomes an event, a repeat does not
become a second event, the arithmetic that produced the row is in the row, and a
go-around is described as normal operations rather than dressed up as an incident.
"""
import pytest

import detect
import promote


class FakeRules:
    min_separation_nm = 3.0
    min_vertical_ft = 1000


AERODROMES = [
    {"icao": "WSSS", "name": "Singapore Changi Airport", "lat": 1.3502, "lon": 103.9940},
    {"icao": "WMKK", "name": "Kuala Lumpur International Airport", "lat": 2.7456, "lon": 101.7099},
]


def conflict(**overrides):
    base = {
        "type": "conflict",
        "a": "SIA517",
        "b": "JAL35",
        "icao24_a": "76cd57",
        "icao24_b": "840abc",
        "separation_nm": 2.1,
        "vertical_ft": 975,
        "lat": 1.36,
        "lon": 103.99,
        "altitude_a_ft": 2400,
    }
    base.update(overrides)
    return base


def motion(kind="go_around", **overrides):
    base = {
        "kind": kind,
        "callsign": "SIA1",
        "icao24": "76cd57",
        "detail": "descended to 900 ft, now climbing at 1400 ft/min through 1200 ft",
        "lat": 1.36,
        "lon": 103.99,
        "altitude_ft": 1200,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# loss of separation
# --------------------------------------------------------------------------- #

def test_a_conflict_becomes_a_high_severity_event():
    events = promote.from_conflicts([conflict()], FakeRules(), observed_at=1793352785, aerodromes=AERODROMES)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "loss_of_separation"
    assert event["severity"] == "high"
    assert event["source"] == "derived"
    assert event["place_icao"] == "WSSS"


def test_the_arithmetic_travels_with_the_row():
    event = promote.from_conflicts([conflict()], FakeRules(), 1793352785, AERODROMES)[0]
    assert event["metrics"]["separation_nm"] == 2.1
    assert event["metrics"]["vertical_ft"] == 975
    assert event["metrics"]["min_separation_nm"] == 3.0
    assert "2.1 NM apart" in event["provenance"]
    assert "975 ft" in event["provenance"]


def test_unknown_vertical_separation_is_stated_not_assumed():
    event = promote.from_conflicts([conflict(vertical_ft=None)], FakeRules(), 1793352785)[0]
    assert "vertical separation unknown" in event["provenance"]


def test_pair_reference_is_order_independent():
    first = promote.from_conflicts([conflict()], FakeRules(), 1793352785)[0]["external_ref"]
    flipped = promote.from_conflicts([conflict(a="JAL35", b="SIA517")], FakeRules(), 1793352785)[0]["external_ref"]
    assert first == flipped


def test_the_same_pair_within_the_bucket_is_the_same_record():
    first = promote.from_conflicts([conflict()], FakeRules(), 1793352785)[0]["external_ref"]
    second = promote.from_conflicts([conflict()], FakeRules(), 1793352785 + 60)[0]["external_ref"]
    assert first == second


def test_a_later_bucket_is_a_new_record():
    first = promote.from_conflicts([conflict()], FakeRules(), 1793352785)[0]["external_ref"]
    later = promote.from_conflicts([conflict()], FakeRules(), 1793352785 + promote.BUCKET_SECONDS + 1)[0][
        "external_ref"
    ]
    assert first != later


def test_a_conflict_without_a_position_is_skipped():
    assert promote.from_conflicts([conflict(lat=None, lon=None)], FakeRules(), 1793352785) == []


def test_an_airport_far_away_is_not_named():
    # 300 NM from either reference aerodrome.
    event = promote.from_conflicts([conflict(lat=6.0, lon=100.0)], FakeRules(), 1793352785, AERODROMES)[0]
    assert event["place_icao"] is None


# --------------------------------------------------------------------------- #
# manoeuvres
# --------------------------------------------------------------------------- #

def test_a_go_around_is_recorded_as_normal_operations():
    event = promote.from_motion([motion()], 1793352785, AERODROMES)[0]
    assert event["event_type"] == "go_around"
    assert event["severity"] == "low"
    assert "normal, safe manoeuvre" in event["provenance"]
    assert "not as an incident" in event["provenance"]


def test_an_unstable_approach_is_medium_and_calls_itself_heuristic():
    event = promote.from_motion([motion("unstable_approach")], 1793352785)[0]
    assert event["severity"] == "medium"
    assert "heuristic" in event["provenance"]


def test_holding_patterns_are_not_promoted():
    # A hold is routine station-keeping; storing every one would bury real events.
    assert promote.from_motion([motion("holding_pattern")], 1793352785) == []


def test_motion_reference_falls_back_to_the_callsign():
    event = promote.from_motion([motion(icao24=None)], 1793352785)[0]
    assert "SIA1" in event["external_ref"]


# --------------------------------------------------------------------------- #
# the snapshot as a whole
# --------------------------------------------------------------------------- #

def test_predictions_never_become_events():
    payload = {
        "observed_at": 1793352785,
        "detections": {"conflicts": []},
        "predicted": {"conflicts": [{"a": "X", "b": "Y", "predicted_nm": 0.4, "time_to_cpa_s": 30}]},
        "motion": {"events": []},
    }
    assert promote.from_snapshot(payload, FakeRules(), AERODROMES) == []


def test_a_snapshot_promotes_both_kinds():
    payload = {
        "observed_at": 1793352785,
        "detections": {"conflicts": [conflict()]},
        "motion": {"events": [motion(), motion("holding_pattern")]},
    }
    events = promote.from_snapshot(payload, FakeRules(), AERODROMES)
    assert sorted(event["event_type"] for event in events) == ["go_around", "loss_of_separation"]


def test_a_snapshot_with_nothing_wrong_produces_nothing():
    payload = {"observed_at": 1793352785, "detections": {"conflicts": []}, "motion": {"events": []}}
    assert promote.from_snapshot(payload, FakeRules(), AERODROMES) == []


def test_promoted_events_are_accepted_by_the_store():
    # The store validates before writing; a derived row must pass the same door.
    import store

    event = promote.from_conflicts([conflict()], FakeRules(), 1793352785, AERODROMES)[0]
    normalised = store.normalise_event(event)
    assert normalised["source"] == "derived"
    assert normalised["callsigns"] == ["JAL35", "SIA517"]
    assert normalised["event_time"].tzinfo is not None


def test_a_real_rules_object_is_understood():
    rules = detect.Rules(min_separation_nm=5.0, min_vertical_ft=800)
    event = promote.from_conflicts([conflict()], rules, 1793352785)[0]
    assert event["metrics"]["min_separation_nm"] == 5.0
    assert "5.0 NM" in event["provenance"]
