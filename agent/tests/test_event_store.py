"""Unit tests for store.py and seed_events.py — no database required.

Same shape as tests/test_nl2sql.py: the things worth protecting are the pure
functions, because they decide what reaches the map. A wrong coordinate or a
filter that silently drops a clause is not a test failure you notice later; it is
a dot in the wrong place that somebody acts on.
"""
from datetime import datetime, timezone

import pytest

import seed_events
import store


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def _event(**overrides):
    base = {
        "event_time": datetime(2025, 1, 22, 9, 30, tzinfo=timezone.utc),
        "event_type": "Runway Incursion",
        "severity": "Level 2",
        "lat": 1.35019,
        "lon": 103.994003,
        "source": "report",
        "provenance": "test",
    }
    base.update(overrides)
    return base


def test_event_type_and_severity_are_slugged():
    row = store.normalise_event(_event())
    assert row["event_type"] == "runway_incursion"
    # 'Level 2' is a finding level, not one of ours — it must not pass through as a severity.
    assert row["severity"] == "unknown"


def test_naive_time_is_treated_as_utc_not_local():
    row = store.normalise_event(_event(event_time=datetime(2025, 1, 22, 9, 30)))
    assert row["event_time"] == datetime(2025, 1, 22, 9, 30, tzinfo=timezone.utc)


def test_aware_time_is_converted_to_utc():
    from datetime import timedelta

    tz8 = timezone(timedelta(hours=8))
    row = store.normalise_event(_event(event_time=datetime(2025, 1, 22, 9, 30, tzinfo=tz8)))
    assert row["event_time"].hour == 1


@pytest.mark.parametrize("lat,lon", [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)])
def test_out_of_range_coordinates_are_refused(lat, lon):
    with pytest.raises(ValueError):
        store.normalise_event(_event(lat=lat, lon=lon))


def test_missing_coordinates_are_refused():
    with pytest.raises(ValueError):
        store.normalise_event({"event_time": datetime.now(timezone.utc), "source": "report"})


def test_unknown_source_is_refused():
    with pytest.raises(ValueError):
        store.normalise_event(_event(source="guesswork"))


def test_callsign_accepts_a_bare_string_or_a_list():
    assert store.normalise_event(_event(callsigns="SIA123"))["callsigns"] == ["SIA123"]
    assert store.normalise_event(_event(callsigns=["SIA123", "MAS88"]))["callsigns"] == ["SIA123", "MAS88"]


# --------------------------------------------------------------------------- #
# query building
# --------------------------------------------------------------------------- #

def test_empty_filters_select_everything_newest_first():
    # Every unfiltered view still means "the active picture": invalidated rows are kept
    # for the audit trail and are opted into explicitly, never shown by accident.
    sql, params = store.build_query()
    assert "WHERE invalidated_at IS NULL" in sql
    assert "ORDER BY event_time DESC" in sql
    assert params == [500]


def test_bbox_becomes_four_parameters_not_sql_text():
    sql, params = store.build_query(bbox=(1.20, 103.60, 1.50, 104.10), limit=10)
    assert "lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s" in sql
    # order is min_lat, max_lat, min_lon, max_lon — a transposed pair here is a
    # correct-looking query that returns the wrong hemisphere.
    assert params == [1.20, 1.50, 103.60, 104.10, 10]


def test_every_user_supplied_value_is_a_parameter():
    sql, params = store.build_query(place_icao="wsss", types=["Runway Incursion"], source="report")
    assert "WSSS" not in sql and "Runway Incursion" not in sql
    assert "WSSS" in params and ["runway_incursion"] in params


def test_inverted_bbox_is_rejected():
    with pytest.raises(ValueError, match="greater than max_lat"):
        store.build_query(bbox=(1.5, 103.6, 1.2, 104.1))


def test_antimeridian_bbox_is_rejected_rather_than_silently_empty():
    with pytest.raises(ValueError, match="antimeridian"):
        store.build_query(bbox=(-10, 179.0, 10, -179.0))


def test_time_window_must_make_sense():
    with pytest.raises(ValueError, match="since must be earlier"):
        store.build_query(
            since=datetime(2025, 3, 1, tzinfo=timezone.utc),
            until=datetime(2025, 2, 1, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("limit", [0, 5001, -3])
def test_limit_bounds(limit):
    with pytest.raises(ValueError):
        store.build_query(limit=limit)


def test_bad_order_rejected():
    with pytest.raises(ValueError):
        store.build_query(order="sideways")


def test_summary_describes_what_was_counted():
    text = store.summarise_filters(bbox=(1.2, 103.6, 1.5, 104.1), types=["runway_incursion"])
    assert "runway_incursion" in text and "1.20,103.60 to 1.50,104.10" in text
    assert store.summarise_filters() == "all events"


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #

def test_synthetic_occurrences_parse():
    rows = seed_events.parse_occurrences()
    assert len(rows) >= 25
    first = rows[0]
    assert first["Occurrence_ID"].startswith("OCC-")
    # A NULL in the source must arrive as None, not the string "NULL".
    nulls = [r for r in rows if r["Aircraft_Registration"] is None]
    assert nulls, "expected at least one row with a NULL registration"


def test_every_row_that_can_be_placed_is_placed():
    events, skipped = seed_events.build_events()
    assert skipped == []
    assert len(events) >= 25


def test_seeding_is_deterministic():
    first, _ = seed_events.build_events()
    second, _ = seed_events.build_events()
    assert [(e["external_ref"], e["event_time"], e["lat"], e["lon"]) for e in first] == [
        (e["external_ref"], e["event_time"], e["lat"], e["lon"]) for e in second
    ]


def test_derived_fields_are_disclosed_in_provenance():
    events, _ = seed_events.build_events()
    for event in events:
        assert "time-of-day derived" in event["provenance"]
        assert "OurAirports" in event["provenance"]


def test_activity_codes_map_to_canonical_types():
    events, _ = seed_events.build_events()
    kinds = {e["event_type"] for e in events}
    assert kinds <= set(store.EVENT_TYPES)
    assert "runway_incursion" in kinds and "bird_strike" in kinds


def test_unknown_location_is_skipped_not_guessed():
    aerodromes, aliases = seed_events.load_aerodromes()
    row = {
        "Occurrence_ID": "OCC-TEST-0001",
        "Occurrence_Date": "2025-01-01",
        "Activity_Code": "Bird Strike",
        "Location": "XXXX",
        "Finding_Level": "Level 2",
    }
    assert seed_events.to_event(row, aerodromes, aliases) is None


def test_known_alias_resolves_and_says_so():
    aerodromes, aliases = seed_events.load_aerodromes()
    row = {
        "Occurrence_ID": "OCC-TEST-0002",
        "Occurrence_Date": "2025-01-01",
        "Activity_Code": "Runway Incursion",
        "Location": "WSMC",
        "Finding_Level": "Level 3",
    }
    event = seed_events.to_event(row, aerodromes, aliases)
    assert event is not None
    assert event["place_name"] == aerodromes["WSAP"]["name"]
    assert "WSMC" in event["provenance"]


def test_points_near_the_aerodrome_not_on_top_of_it():
    aerodromes, aliases = seed_events.load_aerodromes()
    events, _ = seed_events.build_events()
    for event in events:
        base = aerodromes.get(event["place_icao"])
        if base is None:  # aliased codes report the code as filed
            base = aerodromes["WSAP"]
        # jitter is exactly +/-0.02 at the extreme, so compare against that plus a hair
        assert abs(event["lat"] - base["lat"]) <= 0.0201
        assert abs(event["lon"] - base["lon"]) <= 0.0201


def test_severity_mapping_keeps_level_1_as_most_serious():
    events, _ = seed_events.build_events()
    severe = [e for e in events if e["severity"] == "critical"]
    assert severe, "expected at least one Level 1 finding"
    assert all(e["raw"].get("Finding_Level") == "Level 1" for e in severe)


# --------------------------------------------------------------------------- #
# which witness produced a row
# --------------------------------------------------------------------------- #

def _derived_event(**overrides):
    import datetime as dt

    base = {
        "event_time": dt.datetime.now(dt.timezone.utc),
        "event_type": "loss_of_separation",
        "severity": "high",
        "lat": 1.3,
        "lon": 103.9,
        "source": "derived",
    }
    base.update(overrides)
    return base


def test_the_detector_is_recorded_once_at_insert():
    """Comparing yields is worthless if each reader guesses the split differently."""
    snapshot = store.normalise_event(_derived_event(metrics={"detector": "snapshot_separation"}))
    engine = store.normalise_event(_derived_event(metrics={"engine": "openaria-0.5.0"}))
    motion = store.normalise_event(_derived_event(metrics={"detector": "track_motion"}))

    assert snapshot["detector"] == "snapshot_separation"
    assert engine["detector"] == "openaria"
    assert motion["detector"] == "snapshot_motion"


def test_an_unmarked_derived_event_is_unknown_rather_than_guessed():
    assert store.normalise_event(_derived_event())["detector"] == "unknown"


def test_reported_events_are_not_called_detector_output():
    event = store.normalise_event(_derived_event(source="report"))
    assert event["detector"] == "reported"


def test_invalidated_rows_are_out_of_the_active_view_by_default():
    sql, params = store.build_query()
    assert "invalidated_at IS NULL" in sql
    assert "invalidated_at IS NULL" not in store.build_query(include_invalidated=True)[0]


def test_the_map_can_be_filtered_to_one_detector():
    sql, params = store.build_query(detector="openaria")
    assert "detector = %s" in sql
    assert "openaria" in params
