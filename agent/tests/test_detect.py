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


def test_an_aircraft_rolling_out_is_ground_traffic_even_if_the_flag_says_airborne():
    """SIA7403: 0 ft, 147 kt, on_ground false, 0.93 NM from an aircraft on short final.

    The flag was wrong; the height was not. That pair was reported as a loss of
    separation, and it was an arrival sequence.
    """
    pair = [
        aircraft("SIA188", 1.350, 103.990, 225, speed=150),
        aircraft("SIA7403", 1.362, 103.990, 0, speed=147, on_ground=False),
    ]
    assert detect.evaluate(pair, detect.Rules())["conflicts"] == []
    # Switching the rule off still shows it: the arithmetic is not hidden, only the
    # default verdict changes.
    assert len(detect.evaluate(pair, detect.Rules(ignore_ground=False))["conflicts"]) == 1


def test_the_flat_floor_is_gone_and_the_phase_classifier_decides():
    """The 100 ft floor was the first safeguard; it is now one input to a phase call.

    A level aircraft a hundred feet up is a rollout, whatever its flag says. The same
    aircraft descending hard is on approach and stays in the comparison — which is the
    case the flat floor would have discarded.
    """
    rollout = aircraft("LOW1", 1.350, 103.990, 120)
    rollout["vertical_rate_fpm"] = 0
    on_approach = aircraft("LOW2", 1.350, 103.990, 120)
    on_approach["vertical_rate_fpm"] = -700

    assert detect.is_airborne(rollout) is False
    assert detect.is_airborne(on_approach) is True


def test_unknown_altitude_still_reports_proximity():
    pair = [aircraft("AAL1", 1.350, 103.990, None), aircraft("AAL2", 1.355, 103.990, 7000)]
    result = detect.evaluate(pair, detect.Rules())
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["vertical_ft"] is None


# --------------------------------------------------------------------------- #
# speed
# --------------------------------------------------------------------------- #

def test_overspeed_is_flagged_with_its_speed():
    result = detect.evaluate(
        [aircraft("FAST1", 1.35, 103.99, alt=35000, speed=520)], detect.Rules(max_speed_kt=480)
    )
    assert len(result["overspeed"]) == 1
    assert result["overspeed"][0]["ground_speed_kt"] == 520
    assert result["overspeed"][0]["over_by_kt"] == 40


def test_at_the_limit_is_not_over_it():
    result = detect.evaluate(
        [aircraft("EDGE1", 1.35, 103.99, alt=35000, speed=480)], detect.Rules(max_speed_kt=480)
    )
    assert result["overspeed"] == []


# --------------------------------------------------------------------------- #
# banded speed limits
# --------------------------------------------------------------------------- #

def test_cruise_speed_is_legal_high_and_illegal_low():
    # The bug this fixes: one global number flagged all of cruise as overspeeding.
    rules = detect.Rules(max_speed_kt=480)
    high = detect.evaluate([aircraft("HIGH", 1.35, 103.99, alt=35000, speed=470)], rules)
    low = detect.evaluate([aircraft("LOW", 1.35, 103.99, alt=2000, speed=300)], rules)
    assert high["overspeed"] == []
    assert len(low["overspeed"]) == 1
    assert low["overspeed"][0]["limit_kt"] == 250


def test_band_boundary_is_exclusive_below():
    rules = detect.Rules(max_speed_kt=480)
    assert rules.limit_for(9999).max_speed_kt == 250
    assert rules.limit_for(10000).max_speed_kt == 480


def test_unknown_altitude_gets_the_permissive_band():
    rules = detect.Rules(max_speed_kt=480)
    assert rules.limit_for(None).max_speed_kt == 480
    result = detect.evaluate([aircraft("NOSPD", 1.35, 103.99, alt=None, speed=420)], rules)
    assert result["overspeed"] == []


def test_overspeed_names_the_band_it_broke():
    result = detect.evaluate(
        [aircraft("LOW", 1.35, 103.99, alt=3000, speed=280)], detect.Rules(max_speed_kt=480)
    )
    assert result["overspeed"][0]["band"] == "250 kt below 10,000 ft"
    assert "below 10,000 ft" in result["summary"]


def test_custom_bands_round_trip_through_storage():
    rules = detect.Rules.from_json(
        {"speed_bands": [{"max_alt_ft": 5000, "max_speed_kt": 200}, {"max_alt_ft": None, "max_speed_kt": 500}]}
    )
    assert [band.max_speed_kt for band in rules.bands()] == [200, 500]
    assert rules.limit_for(4000).max_speed_kt == 200
    assert rules.limit_for(40000).max_speed_kt == 500


def test_bands_gain_an_open_ended_top_if_missing():
    rules = detect.Rules.from_json({"speed_bands": [{"max_alt_ft": 5000, "max_speed_kt": 200}]})
    assert rules.bands()[-1].max_alt_ft is None


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
