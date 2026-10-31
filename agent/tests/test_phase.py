"""Tests for the phase classifier.

The flat altitude floor was the first safeguard, and it was the wrong instrument in both
directions: it kept rollouts out of the picture (right) while standing ready to discard a
genuine encounter low on climb-out (wrong). These tests pin the three phases to the
positions that actually appear in this feed, including the SIA7403 case that started it.
"""
import phase


def craft(**overrides):
    base = {
        "icao24": "76ce87",
        "altitude_ft": 3000,
        "ground_speed_kt": 250,
        "vertical_rate_fpm": 0,
        "on_ground": False,
    }
    base.update(overrides)
    return base


def test_the_transponder_flag_still_wins():
    assert phase.classify(craft(on_ground=True, altitude_ft=0)) == phase.GROUND


def test_a_rollout_mislabelled_airborne_is_ground():
    """SIA7403: 0 ft, 147 kt, flagged airborne. It was landing, not flying."""
    assert phase.classify(craft(altitude_ft=0, ground_speed_kt=147, vertical_rate_fpm=0)) == phase.GROUND


def test_nothing_flies_level_at_a_hundred_feet_and_a_hundred_and_fifty_knots():
    """The rollout one second after touchdown: fast, low, and not descending any more."""
    assert phase.classify(craft(altitude_ft=75, ground_speed_kt=151, vertical_rate_fpm=65)) == phase.GROUND
    assert phase.classify(craft(altitude_ft=125, ground_speed_kt=102, vertical_rate_fpm=-128)) == phase.GROUND


def test_taxiing_is_ground_however_the_flag_reads():
    assert phase.classify(craft(altitude_ft=60, ground_speed_kt=8, vertical_rate_fpm=0)) == phase.GROUND


def test_a_landing_aircraft_at_the_same_height_is_still_flying():
    """The case the floor would have thrown away: 125 ft and descending hard."""
    assert phase.classify(craft(altitude_ft=125, ground_speed_kt=148, vertical_rate_fpm=-640)) == phase.TRANSITION


def test_a_departure_climbing_through_a_hundred_feet_is_still_flying():
    assert phase.classify(craft(altitude_ft=100, ground_speed_kt=160, vertical_rate_fpm=2000)) == phase.TRANSITION


def test_low_and_level_but_not_on_a_runway_is_transition():
    assert phase.classify(craft(altitude_ft=900, ground_speed_kt=180, vertical_rate_fpm=0)) == phase.TRANSITION


def test_established_traffic_is_airborne():
    assert phase.classify(craft(altitude_ft=30000, ground_speed_kt=460)) == phase.AIRBORNE
    assert phase.classify(craft(altitude_ft=5000, vertical_rate_fpm=1200)) == phase.AIRBORNE


def test_an_unknown_height_stays_in_play():
    """No altitude is not a licence to drop the aircraft from the comparison."""
    assert phase.classify(craft(altitude_ft=None)) == phase.AIRBORNE
    assert phase.is_airborne(craft(altitude_ft=None)) is True


def test_only_ground_is_excluded_from_separation():
    assert phase.is_airborne(craft(altitude_ft=0)) is False
    assert phase.is_airborne(craft(altitude_ft=125, vertical_rate_fpm=-640)) is True
    assert phase.is_airborne(craft(altitude_ft=30000)) is True
