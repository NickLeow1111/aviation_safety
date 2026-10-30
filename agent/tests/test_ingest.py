"""Unit tests for ingest.py — turning an uploaded file into map events.

The failure this file guards against is a row that *looks* imported but landed at
the wrong place or the wrong time. Every test here is a claim the preview makes to
the user, checked without a database or an HTTP server.
"""
from datetime import datetime, timezone

import pytest

import ingest


@pytest.fixture(scope="module")
def resolver():
    return ingest.LocationResolver()


# --------------------------------------------------------------------------- #
# coordinate parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.3502", 1.3502),
        ("1.3502N", 1.3502),
        ("1.3502S", -1.3502),
        ("103.994E", 103.994),
        ("-84.4281", -84.4281),
        ("33.6367N", 33.6367),
        ('1°21\'0.7"N', 1.350194),
        ("103°59'38.4\"E", 103.994),
        ("1°21.0'N", 1.35),
        ("", None),
        ("WSSS", None),
        ("north", None),
    ],
)
def test_single_coordinates(text, expected):
    value = ingest.parse_coordinate(text)
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected, abs=1e-5)


def test_dms_actually_converts():
    # 1 deg 21 min 0.7 sec = 1 + 21/60 + 0.7/3600
    assert ingest.parse_coordinate("1°21'0.7\"N") == pytest.approx(1 + 21 / 60 + 0.7 / 3600, abs=1e-6)


@pytest.mark.parametrize(
    "text",
    [
        "1.3502, 103.994",
        "1.3502 103.994",
        "1.3502N 103.994E",
        '1°21\'0.7"N 103°59\'38.4"E',
        "1°21'0.7\"N, 103°59'38.4\"E",
    ],
)
def test_coordinate_pairs(text):
    pair = ingest.parse_coordinate_text(text)
    assert pair is not None
    assert pair[0] == pytest.approx(1.35, abs=1e-3)
    assert pair[1] == pytest.approx(103.994, abs=1e-3)


def test_null_island_is_refused():
    # A blank spreadsheet cell pair, the most common bad coordinate in the wild.
    assert ingest.parse_coordinate_text("0, 0") is None
    assert ingest.parse_coordinate_text("0 0") is None


def test_out_of_range_pair_is_refused():
    assert ingest.parse_coordinate_text("120.0, 200.0") is None


def test_location_column_may_hold_coordinates():
    row = {"location": '1°21\'0.7"N 103°59\'38.4"E', "lat": "", "lon": ""}
    resolver = ingest.LocationResolver()
    lat, lon, method, icao, name, note, errors = ingest.resolve_position(row, resolver)
    assert errors == []
    assert method == "coordinates"
    assert icao is None and name is None
    assert lat == pytest.approx(1.350194, abs=1e-6)


# --------------------------------------------------------------------------- #
# time parsing
# --------------------------------------------------------------------------- #

def test_iso_datetime_keeps_its_zone():
    when, source = ingest.parse_when("2026-10-12T23:30:00+08:00")
    assert source == "datetime"
    assert when == datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc)


def test_date_plus_time_is_utc():
    when, source = ingest.parse_when("2026-10-12", "09:14")
    assert source == "datetime"
    assert when == datetime(2026, 10, 12, 9, 14, tzinfo=timezone.utc)


def test_date_only_is_flagged_not_guessed():
    when, source = ingest.parse_when("2026-10-13", "")
    assert source == "date_only"
    assert when == datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)


def test_day_first_dates_are_read():
    when, source = ingest.parse_when("13/10/2026", "08:05")
    assert source == "datetime"
    assert when.day == 13 and when.month == 10


def test_unreadable_date_is_reported():
    when, source = ingest.parse_when("sometime in October", "")
    assert when is None
    assert source == "unparseable"


# --------------------------------------------------------------------------- #
# file parsing
# --------------------------------------------------------------------------- #

def test_column_aliases_are_understood():
    text = "Occurrence Date,Event Time,Airport,Activity Code,Level,Summary\n2026-10-12,09:14,WSSS,Bird Strike,Level 2,strike on climb\n"
    rows = ingest.parse_table(text, "x.csv")
    assert rows[0]["date"] == "2026-10-12"
    assert rows[0]["time"] == "09:14"
    assert rows[0]["location"] == "WSSS"
    assert rows[0]["type"] == "Bird Strike"
    assert rows[0]["severity"] == "Level 2"
    assert rows[0]["description"] == "strike on climb"


def test_blank_lines_are_ignored():
    text = "date,location\n2026-10-12,WSSS\n\n,,\n"
    rows = ingest.parse_table(text, "x.csv")
    assert len(rows) == 1


def test_excel_is_refused_with_advice():
    with pytest.raises(ValueError, match="CSV"):
        ingest.parse_upload(b"PK\x03\x04", "events.xlsx")


def test_bom_is_stripped():
    text = "\ufeffdate,location\n2026-10-12,WSSS\n"
    rows = ingest.parse_upload(text.encode("utf-8"), "x.csv")
    assert rows[0]["date"] == "2026-10-12"


# --------------------------------------------------------------------------- #
# rows -> events
# --------------------------------------------------------------------------- #

def test_icao_row_resolves(resolver):
    rows = [{"date": "2026-10-12", "time": "09:14", "location": "WSSS", "type": "runway_incursion"}]
    parsed = ingest.build_events(rows, resolver)
    assert parsed[0].errors == []
    event = parsed[0].event
    assert event["position_source"] == "icao"
    assert event["place_icao"] == "WSSS"
    assert event["lat"] == pytest.approx(1.35019, abs=1e-5)


def test_alias_is_reported_as_an_alias(resolver):
    rows = [{"date": "2026-10-14", "time": "22:10", "location": "WSMC", "type": "runway_incursion"}]
    parsed = ingest.build_events(rows, resolver)
    assert parsed[0].errors == []
    assert parsed[0].event["position_source"] == "alias"
    assert parsed[0].event["position_note"] and "WSMC" in parsed[0].event["position_note"]


def test_unknown_location_fails_the_row_not_the_file(resolver):
    rows = [
        {"date": "2026-10-16", "time": "06:45", "location": "XXXX", "type": "other"},
        {"date": "2026-10-16", "time": "07:45", "location": "WSSS", "type": "other"},
    ]
    parsed = ingest.build_events(rows, resolver)
    assert parsed[0].errors and parsed[1].errors == []
    assert parsed[0].event is None


def test_missing_time_does_not_become_midnight_silently(resolver):
    rows = [{"date": "2026-10-13", "time": "", "location": "WSSS", "type": "go_around"}]
    parsed = ingest.build_events(rows, resolver)
    assert parsed[0].event["time_source"] == "date_only"


def test_near_without_bearing_is_refused(resolver):
    rows = [{"date": "2026-10-13", "time": "10:00", "location": "near WSSS 5NM", "type": "bird_strike"}]
    parsed = ingest.build_events(rows, resolver)
    assert parsed[0].errors and "bearing" in parsed[0].errors[0]


def test_near_without_distance_resolves_the_aerodrome(resolver):
    rows = [{"date": "2026-10-13", "time": "10:00", "location": "near WSSS", "type": "bird_strike"}]
    parsed = ingest.build_events(rows, resolver)
    assert parsed[0].errors == []
    assert parsed[0].event["position_source"] == "near"


def test_store_rows_carry_provenance_and_a_stable_key(resolver):
    rows = [{"date": "2026-10-12", "time": "09:14", "location": "WSSS", "type": "runway_incursion", "description": "d"}]
    parsed = ingest.build_events(rows, resolver)
    first = ingest.to_store_events(parsed, "a.csv")[0]
    second = ingest.to_store_events(ingest.build_events(rows, resolver), "a.csv")[0]

    assert first["external_ref"] == second["external_ref"]
    assert first["provenance"].startswith("uploaded file a.csv")
    assert "position from icao" in first["provenance"]
    assert first["source"] == "upload"


def test_geodesic_offset_is_sane():
    lat, lon = ingest.offset_nm(1.35, 103.99, 0.0, 60.0)
    assert lat == pytest.approx(2.35, abs=0.02)  # one degree of latitude is ~60 NM
    assert lon == pytest.approx(103.99, abs=0.01)
