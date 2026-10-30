"""Tests for predicted separation and track-based manoeuvre detection.

Both features answer questions about the future or the recent past, which is exactly
the sort of code that quietly reports nonsense. Each test therefore pins a physical
situation — converging, diverging, vertically separated, a go-around, a hold — rather
than asserting that a function returns a list.
"""
import math

import pytest

import detect
import predict
import tracks


@pytest.fixture(autouse=True)
def clean_tracks():
    """The store is module-level by design; tests must not inherit each other's."""
    tracks.reset()
    yield
    tracks.reset()


def plane(**overrides):
    base = {
        "icao24": "abc",
        "callsign": "SIA1",
        "lat": 1.30,
        "lon": 103.90,
        "altitude_ft": 10000,
        "ground_speed_kt": 400,
        "track_deg": 90.0,
        "vertical_rate_fpm": 0.0,
        "on_ground": False,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# projection geometry
# --------------------------------------------------------------------------- #

def test_projection_moves_along_the_track():
    # 360 kt for 60 s is 6 NM north when tracking 000.
    lat, lon = predict.project(1.30, 103.90, 0.0, 360.0, 60.0)
    assert math.isclose(lat, 1.30 + 6.0 / 60.0, rel_tol=1e-6)
    assert math.isclose(lon, 103.90, rel_tol=1e-6)


def test_projection_is_zero_distance_with_no_speed():
    assert predict.project(1.30, 103.90, 90.0, None, 60.0) == (1.30, 103.90)


def test_separation_at_the_equator_is_sixty_miles_a_degree():
    assert math.isclose(predict.separation_nm(0.0, 0.0, 0.0, 1.0), 60.0, rel_tol=1e-3)


def test_head_on_aircraft_are_predicted_to_converge():
    rules = detect.Rules()
    east = plane(callsign="EAST1", lat=1.30, lon=103.90, track_deg=90.0)
    west = plane(icao24="b", callsign="WEST2", lat=1.30, lon=104.00, track_deg=270.0)
    result = predict.evaluate([east, west], rules)
    assert len(result["conflicts"]) == 1
    pair = result["conflicts"][0]
    assert pair["predicted_nm"] < pair["current_nm"]
    # 6 NM closing at 800 kt (13.3 NM a minute) lands inside two minutes.
    assert 0 < pair["time_to_cpa_s"] <= 120


def test_parallel_traffic_is_not_a_conflict():
    rules = detect.Rules()
    result = predict.evaluate(
        [plane(track_deg=90.0), plane(icao24="b", lon=104.00, track_deg=90.0)], rules
    )
    assert result["conflicts"] == []


def test_vertical_separation_prevents_a_prediction():
    rules = detect.Rules()
    result = predict.evaluate(
        [plane(track_deg=90.0), plane(icao24="b", lon=104.00, track_deg=270.0, altitude_ft=14000)], rules
    )
    assert result["conflicts"] == []


def test_aircraft_already_conflicting_are_left_to_the_snapshot_check():
    # Two aircraft parked next to each other cannot converge further; the snapshot
    # detector reports them and a duplicate here would double-count.
    rules = detect.Rules()
    result = predict.evaluate(
        [plane(icao24="a", lat=1.30, lon=103.900), plane(icao24="b", lat=1.30, lon=103.905, track_deg=270.0)],
        rules,
    )
    assert result["conflicts"] == []


def test_distant_pairs_are_not_projected():
    rules = detect.Rules()
    far = plane(icao24="b", lon=105.50, track_deg=270.0)  # ~90 NM away
    assert predict.evaluate([plane(), far], rules)["conflicts"] == []


def test_ground_aircraft_are_ignored():
    rules = detect.Rules()
    result = predict.evaluate(
        [plane(on_ground=True), plane(icao24="b", lon=104.00, track_deg=270.0, on_ground=True)], rules
    )
    assert result["conflicts"] == []


def test_projection_says_that_it_is_a_projection():
    assert "not a radar conflict alert" in predict.evaluate([], detect.Rules())["disclaimer"]


# --------------------------------------------------------------------------- #
# track history
# --------------------------------------------------------------------------- #

def sample(alt, vs, lat=1.30, track=20.0, speed=150, on_ground=False):
    return {
        "icao24": "abc",
        "callsign": "GA1",
        "lat": lat,
        "lon": 103.90,
        "altitude_ft": alt,
        "ground_speed_kt": speed,
        "track_deg": track,
        "vertical_rate_fpm": vs,
        "on_ground": on_ground,
    }


def feed(rows, start=1000.0, gap=15.0):
    for index, row in enumerate(rows):
        tracks.record([row], now=start + index * gap)


def test_go_around_is_detected_from_the_climb_away():
    feed([sample(3000, -900), sample(900, -800), sample(1200, 1400)])
    events = tracks.motion_events()["events"]
    assert [event["kind"] for event in events] == ["go_around"]
    assert "climbing" in events[0]["detail"]


def test_ordinary_descent_is_not_a_go_around():
    feed([sample(3000, -900), sample(2000, -900), sample(1200, -800)])
    assert tracks.motion_events()["events"] == []


def test_a_partly_turned_circle_is_not_a_hold():
    feed([sample(8000, 0, track=0), sample(8000, 0, track=60), sample(8000, 0, track=120)])
    assert "holding_pattern" not in [e["kind"] for e in tracks.motion_events()["events"]]


def test_a_circle_in_one_place_is_a_hold():
    tracks_track = [0, 90, 180, 270, 350]
    feed([sample(8000, 0, track=float(angle)) for angle in tracks_track])
    kinds = [event["kind"] for event in tracks.motion_events()["events"]]
    assert "holding_pattern" in kinds


def test_turning_while_travelling_is_not_a_hold():
    rows = [
        sample(8000, 0, lat=1.30 + index * 0.08, track=float(index * 90 % 360))
        for index in range(5)
    ]
    feed(rows)
    assert "holding_pattern" not in [e["kind"] for e in tracks.motion_events()["events"]]


def test_fast_low_approach_is_unstable():
    feed([sample(3000, -700, speed=200), sample(2000, -700, speed=210), sample(800, -600, speed=205)])
    kinds = [event["kind"] for event in tracks.motion_events()["events"]]
    assert "unstable_approach" in kinds


def test_normal_short_final_is_not_flagged():
    feed([sample(3000, -700, speed=160), sample(2000, -700, speed=150), sample(800, -600, speed=140)])
    assert tracks.motion_events()["events"] == []


def test_no_history_means_no_manoeuvre_claims():
    tracks.reset()
    assert tracks.motion_events()["events"] == []


def test_single_sample_is_not_enough_to_judge_motion():
    tracks.record([sample(900, 500)])
    assert tracks.motion_events()["events"] == []


def test_history_is_pruned_to_the_window():
    tracks.reset()
    tracks.record([sample(1000, 0)], now=1000.0)
    assert tracks.tracked_count() == 1
    tracks.record([sample(1000, 0)], now=1000.0 + tracks.HISTORY_SECONDS + 1)
    assert tracks.tracked_count() == 1  # the old aircraft aged out, this one is new
    assert len(tracks.history("abc")) == 1


def test_samples_without_a_position_are_not_recorded():
    tracks.reset()
    tracks.record([{"icao24": "x", "lat": None, "lon": None}])
    assert tracks.tracked_count() == 0
