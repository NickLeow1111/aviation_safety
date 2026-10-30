"""Unit tests for the unusual-flight heuristics.

These decide what an analyst looks at first, so the bar is: a normal flight must
produce nothing, and each signal must name the aircraft and the reason. A test that
only proves flags fire would let a detector that flags everything pass.
"""
import pytest

import anomaly


@pytest.fixture(autouse=True)
def clean_history():
    anomaly.reset_history()
    yield
    anomaly.reset_history()


def plane(**overrides):
    base = {
        "icao24": "abc123",
        "callsign": "SIA1",
        "lat": 1.35,
        "lon": 103.99,
        "altitude_ft": 35000,
        "ground_speed_kt": 450,
        "vertical_rate_fpm": 0,
        "squawk": "1234",
        "on_ground": False,
        "track_deg": 90.0,
    }
    base.update(overrides)
    return base


def kinds(result):
    return [flag["kind"] for flag in result["flags"]]


def test_ordinary_cruise_produces_nothing():
    result = anomaly.find([plane()])
    assert result["flags"] == []
    assert "No unusual" in result["summary"]


def test_emergency_squawk_is_flagged_with_its_meaning():
    result = anomaly.find([plane(squawk="7700")])
    assert kinds(result) == ["emergency_squawk"]
    assert "general emergency" in result["flags"][0]["detail"]


def test_all_three_emergency_codes_are_recognised():
    for code in ("7500", "7600", "7700"):
        anomaly.reset_history()
        assert "emergency_squawk" in kinds(anomaly.find([plane(squawk=code)]))


def test_ordinary_squawk_is_not_an_emergency():
    assert anomaly.find([plane(squawk="7000")])["flags"] == []


def test_fast_and_low_is_flagged():
    result = anomaly.find([plane(altitude_ft=900, ground_speed_kt=300)])
    assert kinds(result) == ["low_and_fast"]
    assert "300 kt at 900 ft" in result["flags"][0]["detail"]


def test_low_but_slow_is_not_flagged():
    # Short final: low and slow is ordinary.
    assert anomaly.find([plane(altitude_ft=900, ground_speed_kt=140)])["flags"] == []


def test_extreme_descent_is_flagged_with_a_direction():
    result = anomaly.find([plane(vertical_rate_fpm=-7200)])
    assert kinds(result) == ["extreme_vertical_rate"]
    assert "descending" in result["flags"][0]["detail"]


def test_normal_climb_rate_is_not_flagged():
    assert anomaly.find([plane(vertical_rate_fpm=2400)])["flags"] == []


def test_fast_taxi_on_the_ground_is_flagged():
    result = anomaly.find([plane(on_ground=True, ground_speed_kt=75, altitude_ft=20)])
    assert "fast_on_ground" in kinds(result)


def test_normal_taxi_is_not_flagged():
    assert anomaly.find([plane(on_ground=True, ground_speed_kt=18, altitude_ft=20)])["flags"] == []


def test_slow_at_altitude_is_flagged():
    result = anomaly.find([plane(altitude_ft=4000, ground_speed_kt=45)])
    assert "slow_at_altitude" in kinds(result)


def test_missing_position_is_skipped_not_guessed():
    result = anomaly.find([plane(lat=None, lon=None, squawk="7700")])
    assert result["flags"] == []


def test_summary_counts_signals_and_aircraft():
    result = anomaly.find([plane(squawk="7700", vertical_rate_fpm=9000)])
    assert "2 unusual signal(s)" in result["summary"]
    assert "across 1 aircraft" in result["summary"]


def test_result_carries_the_heuristic_disclaimer():
    assert "do not mean an aircraft is in difficulty" in anomaly.find([])["disclaimer"]


# --------------------------------------------------------------------------- #
# turn detection, which needs two snapshots
# --------------------------------------------------------------------------- #

def test_sharp_turn_between_snapshots_is_flagged():
    clock = [0.0]
    anomaly.find([plane(track_deg=10)], now=lambda: clock[0])
    clock[0] = 20.0
    result = anomaly.find([plane(track_deg=60)], now=lambda: clock[0])
    assert "sharp_turn" in kinds(result)
    assert "turned 50°" in result["flags"][0]["detail"]


def test_gentle_turn_is_not_flagged():
    clock = [0.0]
    anomaly.find([plane(track_deg=10)], now=lambda: clock[0])
    clock[0] = 20.0
    assert "sharp_turn" not in kinds(anomaly.find([plane(track_deg=20)], now=lambda: clock[0]))


def test_turn_across_north_is_measured_the_short_way():
    clock = [0.0]
    anomaly.find([plane(track_deg=350)], now=lambda: clock[0])
    clock[0] = 20.0
    result = anomaly.find([plane(track_deg=10)], now=lambda: clock[0])
    # 350 -> 10 is 20 degrees, not 340: a naive subtraction would flag it.
    assert "sharp_turn" not in kinds(result)


def test_no_turn_flag_without_a_previous_snapshot():
    result = anomaly.find([plane(track_deg=180)], now=lambda: 0.0)
    assert "sharp_turn" not in kinds(result)


def test_stale_history_is_pruned():
    clock = [0.0]
    anomaly.find([plane()], now=lambda: clock[0])
    assert anomaly._previous  # noqa: SLF001 - asserting the internal cache on purpose
    clock[0] = anomaly.HISTORY_TTL_SECONDS + 1
    anomaly.find([plane()], now=lambda: clock[0])
    assert len(anomaly._previous) == 1  # noqa: SLF001
