"""Tests for the dense poller.

The property that matters is not "does it fetch" — it is that an unattended process
cannot inflate the record. A poller that stores the same instant twice draws a
stationary aircraft, and one that spends its budget at a fixed rate goes blind by
midday. So the tests pin the budget arithmetic, the merge rule that decides which
antenna's position wins, and the write gate that stops duplicate fixes.
"""
from datetime import datetime, timedelta, timezone

import pytest

import community_adsb
import poller


# --------------------------------------------------------------------------- #
# budget arithmetic
# --------------------------------------------------------------------------- #

def test_interval_spreads_a_full_budget_across_the_day():
    """3,400 credits over 86,400 seconds is one fix every 25 seconds, 24 hours a day.

    That number is the honest ceiling on our continuous coverage: it is why class-of-2026
    capture cannot match OpenARIA's 4.7 s radar input on OpenSky alone.
    """
    interval = poller.plan_interval(credits_remaining=3400, seconds_remaining=86400, floor=15, ceiling=120)
    assert interval == pytest.approx(25.4, abs=0.1)


def test_interval_never_drops_below_the_politeness_floor():
    interval = poller.plan_interval(credits_remaining=100000, seconds_remaining=86400, floor=15, ceiling=120)
    assert interval == 15, "a huge budget still must not hammer the endpoint"


def test_interval_stretches_as_the_day_runs_out():
    """Ten credits left and three hours to go: one poll per 18 minutes, not one per 25 s."""
    interval = poller.plan_interval(credits_remaining=10, seconds_remaining=3 * 3600, floor=15, ceiling=120)
    assert interval == 120, "the ceiling is what stops it going silent"


def test_interval_mid_budget_lands_between_the_bounds():
    interval = poller.plan_interval(credits_remaining=500, seconds_remaining=10 * 3600, floor=15, ceiling=120)
    assert 15 <= interval <= 120
    assert interval == pytest.approx(72.0)


def test_a_spent_budget_backs_off_instead_of_polling_anyway():
    assert poller.plan_interval(credits_remaining=0, seconds_remaining=3600) == poller.MAX_INTERVAL_SECONDS


def test_seconds_left_counts_to_the_next_utc_midnight():
    late = datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)
    assert poller.seconds_left_utc(late) == pytest.approx(3600, abs=1)


# --------------------------------------------------------------------------- #
# merging two antennas
# --------------------------------------------------------------------------- #

def fix(icao24, lat, lon, age=0.0, source="opensky", altitude=10000):
    return {
        "icao24": icao24,
        "lat": lat,
        "lon": lon,
        "altitude_ft": altitude,
        "observed_at": datetime.now(timezone.utc).timestamp() - age,
        "source": source,
    }


def test_merge_keeps_both_networks_aircraft():
    merged = poller.merge_fixes([fix("aaa", 1.0, 103.0)], [fix("bbb", 2.0, 102.0, source="adsb.lol")])
    assert {m["icao24"] for m in merged} == {"aaa", "bbb"}


def test_merge_prefers_the_fresher_position_without_losing_opensky_fields():
    primary = fix("aaa", 1.00, 103.00, age=10.0, altitude=20000)
    community = fix("aaa", 1.01, 103.01, age=0.5, source="adsb.lol", altitude=20100)
    merged = poller.merge_fixes([primary], [community])
    assert len(merged) == 1
    assert merged[0]["lat"] == 1.01, "the sub-second fix wins on position"
    assert merged[0]["altitude_ft"] == 20100
    assert merged[0]["community_source"] == "adsb.lol"


def test_merge_keeps_opensky_when_the_community_fix_is_not_fresher():
    primary = fix("aaa", 1.00, 103.00, age=0.0)
    community = fix("aaa", 9.99, 103.99, age=5.0, source="adsb.lol")
    merged = poller.merge_fixes([primary], [community])
    assert merged[0]["lat"] == 1.00


# --------------------------------------------------------------------------- #
# the write gate
# --------------------------------------------------------------------------- #

def test_should_store_allows_a_first_fix_and_blocks_a_duplicate():
    last = {}
    assert poller.should_store("aaa", 1000.0, last) is True
    last["aaa"] = 1000.0
    assert poller.should_store("aaa", 1002.0, last) is False, "two sources, seconds apart, one track"
    assert poller.should_store("aaa", 1006.0, last) is True


def test_circle_covers_the_box_and_respects_the_feed_cap():
    lat, lon, radius = poller.circle_for_bbox((0.5, 102.5, 4.5, 107.5))
    assert lat == pytest.approx(2.5)
    assert lon == pytest.approx(105.0)
    assert 25 <= radius <= 250


# --------------------------------------------------------------------------- #
# the community feed's parsing
# --------------------------------------------------------------------------- #

def test_community_payload_becomes_the_same_shape_as_opensky():
    fixes = community_adsb.normalise(
        {
            "ac": [
                {
                    "hex": "7684aa",
                    "flight": "SIA321  ",
                    "lat": 1.35,
                    "lon": 103.99,
                    "alt_baro": 35000,
                    "gs": 480.5,
                    "track": 42.0,
                    "baro_rate": 0,
                    "seen_pos": 0.4,
                }
            ]
        }
    )
    assert len(fixes) == 1
    only = fixes[0]
    assert only["icao24"] == "7684aa"
    assert only["callsign"] == "SIA321"
    assert only["altitude_ft"] == 35000
    assert only["on_ground"] is False
    assert only["source"] == "adsb.lol"


def test_community_reports_a_parked_aircraft_as_on_ground_not_at_an_altitude():
    """`alt_baro` is the string "ground" for aircraft that are not flying."""
    fixes = community_adsb.normalise({"ac": [{"hex": "abc123", "lat": 1.36, "lon": 103.99, "alt_baro": "ground"}]})
    assert fixes[0]["on_ground"] is True
    assert fixes[0]["altitude_ft"] == 0


def test_community_drops_entries_without_a_position():
    """A track with a hole is worse than a track that starts later."""
    assert community_adsb.normalise({"ac": [{"hex": "abc123", "alt_baro": 1000}]}) == []


# --------------------------------------------------------------------------- #
# the lesson from the last unattended writer
# --------------------------------------------------------------------------- #

def test_the_poller_never_recreates_the_store():
    """db/001_safety_event.sql starts with DROP TABLE — a poller must not run it."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(poller))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "apply_schema" not in calls
