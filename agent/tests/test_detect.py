"""Unit tests for the separation and speed checks.

The point of these tests is that a threshold means what it says: an aircraft at
3.01 NM is not inside a 3 NM minimum, and one at 3.00 NM is. Off-by-a-hair in a
separation check is the kind of bug that gets a feature deleted.
"""
import pytest

import detect


def aircraft(callsign, lat, lon, alt=7000, speed=250, on_ground=False, icao24=None):
    return {
        "icao24": icao24 or callsign.lower(),
        "callsign": callsign,
        "lat": lat,
        "lon": lon,
        "altitude_ft": alt,
        "ground_speed_kt": speed,
        "on_ground": on_ground,
    }


# --------------------------------------------------------------------------- #
# distance
# --------------------------------------------------------------------------- #

def test_one_degree_of_latitude_is_sixty_nm():
    assert detect.separation_nm(1.0, 103.0, 2.0, 103.0) == pytest.approx(60.0, abs=0.2)


def test_same_point_is_zero():
    assert detect.separation_nm(1.35, 103.99, 1.35, 103.99) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# separation
# --------------------------------------------------------------------------- #

def test_pair_inside_both_minima_is_a_conflict():
    # ~2.8 NM apart laterally, 400 ft apart vertically: inside 3 NM / 1000 ft.
    result = detect.evaluate(
        [aircraft("AAL1", 1.350, 103.990, 7000), aircraft("AAL2", 1.396, 103.990, 7400)],
        detect.Rules(),
    )
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["separation_nm"] == pytest.approx(2.76, abs=0.05)
    assert conflict["vertical_ft"] == 400


def test_vertical_separation_clears_a_horizontal_proximity():
    # Same lateral position but 3000 ft apart: legal, and must not be reported.
    result = detect.evaluate(
        [aircraft("AAL1", 1.350, 103.990, 7000), aircraft("AAL2", 1.352, 103.990, 10000)],
        detect.Rules(),
    )
    assert result["conflicts"] == []


def test_outside_the_minimum_by_a_hair_is_not_a_conflict():
    # 3.02 NM apart, same altitude.
    result = detect.evaluate(
        [aircraft("AAL1", 1.350, 103.990, 7000), aircraft("AAL2", 1.4003, 103.990, 7000)],
        detect.Rules(min_separation_nm=3.0),
    )
    assert result["conflicts"] == []


def test_rule_change_changes_the_verdict():
    pair = [aircraft("AAL1", 1.350, 103.990, 7000), aircraft("AAL2", 1.400, 103.990, 7000)]
    assert detect.evaluate(pair, detect.Rules(min_separation_nm=3.0))["conflicts"] == []
    assert len(detect.evaluate(pair, detect.Rules(min_separation_nm=5.0))["conflicts"]) == 1


def test_ground_traffic_is_excluded_by_default():
    pair = [
        aircraft("VEH1", 1.350, 103.990, 20, on_ground=True),
        aircraft("VEH2", 1.351, 103.990, 20, on_ground=True),
    ]
    assert detect.evaluate(pair, detect.Rules())["conflicts"] == []
    assert len(detect.evaluate(pair, detect.Rules(ignore_ground=False))["conflicts"]) == 1


def test_unknown_altitude_still_reports_proximity():
    pair = [aircraft("AAL1", 1.350, 103.990, None), aircraft("AAL2", 1.355, 103.990, 7000)]
    result = detect.evaluate(pair, detect.Rules())
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["vertical_ft"] is None


# --------------------------------------------------------------------------- #
# speed
# --------------------------------------------------------------------------- #

def test_overspeed_is_flagged_with_its_speed():
    result = detect.evaluate([aircraft("FAST1", 1.35, 103.99, speed=520)], detect.Rules(max_speed_kt=480))
    assert len(result["overspeed"]) == 1
    assert result["overspeed"][0]["ground_speed_kt"] == 520


def test_at_the_limit_is_not_over_it():
    result = detect.evaluate([aircraft("EDGE1", 1.35, 103.99, speed=480)], detect.Rules(max_speed_kt=480))
    assert result["overspeed"] == []


def test_missing_speed_is_not_assumed_fast():
    result = detect.evaluate([aircraft("NOSPD", 1.35, 103.99, speed=None)], detect.Rules())
    assert result["overspeed"] == []


# --------------------------------------------------------------------------- #
# switches and summaries
# --------------------------------------------------------------------------- #

def test_disabled_rules_do_nothing_and_say_so():
    result = detect.evaluate([aircraft("AAL1", 1.35, 103.99)], detect.Rules(enabled=False))
    assert result["conflicts"] == [] and result["overspeed"] == []
    assert "switched off" in result["summary"]


def test_summary_quotes_the_thresholds_in_force():
    result = detect.evaluate([], detect.Rules(min_separation_nm=4.5, max_speed_kt=430, min_vertical_ft=800))
    assert "4.5 NM / 800 ft" in result["summary"]
    assert "430 kt" in result["summary"]


def test_every_result_carries_the_snapshot_disclaimer():
    result = detect.evaluate([], detect.Rules())
    assert "not a conflict prediction" in result["disclaimer"]


# --------------------------------------------------------------------------- #
# rules from storage and from prose
# --------------------------------------------------------------------------- #

def test_rules_survive_garbage_storage():
    assert detect.Rules.from_json(None) == detect.Rules()
    assert detect.Rules.from_json("not json") == detect.Rules()
    assert detect.Rules.from_json({"min_separation_nm": "abc"}).min_separation_nm == 3.0
    assert detect.Rules.from_json('{"min_separation_nm": 2}').min_separation_nm == 2.0


def test_rules_clamp_nonsense():
    assert detect.Rules.from_json({"min_separation_nm": -5}).min_separation_nm == 0.1
    assert detect.Rules.from_json({"max_speed_kt": 0}).max_speed_kt == 1


@pytest.mark.parametrize(
    "text,field,expected",
    [
        ("flag anything closer than 2.5 nautical miles", "min_separation_nm", 2.5),
        ("set the separation minimum to 3 nm", "min_separation_nm", 3.0),
        ("call it at 4 miles", "min_separation_nm", 3.475),
        ("alert me faster than 500 kt", "max_speed_kt", 500),
        ("set the speed limit to 450 knots", "max_speed_kt", 450),
        ("vertical separation of 800 ft", "min_vertical_ft", 800),
    ],
)
def test_plain_language_thresholds(text, field, expected):
    updated, changes = detect.parse_rules(text)
    assert updated is not None, changes
    assert getattr(updated, field) == pytest.approx(expected, abs=0.01)
    assert changes


def test_a_sentence_without_a_threshold_changes_nothing():
    updated, changes = detect.parse_rules("keep an eye on the separation at Changi")
    assert updated is None
    assert changes == []


def test_detection_can_be_turned_off_by_prose():
    updated, changes = detect.parse_rules("turn off the separation checks", detect.Rules())
    assert updated is not None and updated.enabled is False
    assert "detection off" in changes


def test_parsing_keeps_untouched_fields():
    base = detect.Rules(min_separation_nm=5.0, max_speed_kt=430, min_vertical_ft=900)
    updated, _ = detect.parse_rules("flag anything closer than 2 nm", base)
    assert updated.min_separation_nm == 2.0
    assert updated.max_speed_kt == 430
    assert updated.min_vertical_ft == 900
