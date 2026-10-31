"""Tests for the OpenARIA batch pass.

The failure these guard against is a confident-looking row that OpenARIA did not
actually support: a position invented from a missing field, a repeat that becomes a
second dot on the map, or an encounter graded as an incident on a different agency's
thresholds. So the tests pin the CSV contract, the concatenated-JSON quirk that makes
`json.load` throw "Extra data", idempotent keys, the minima we grade against, and the
fact that every row has to survive the same validation door as an upload.
"""
import json

import pytest

import openaria
import store


def payload(**overrides):
    """A trimmed OpenARIA event, shaped like the real output."""
    base = {
        "uniqueId": "188ab7432c7be8fa09aa4ccdda7ed188",
        "eventScore": 10.45415,
        "eventDate": "2026-10-31",
        "eventTime": "03:51:31.238",
        "eventEpochMsTime": 1793428291238,
        "latitude": 2.3057,
        "longitude": 102.2457,
        "timeToCpaInMilliSec": 0,
        "conflictAngle": "CROSSING",
        "atClosestLateral": {"trueLateralNm": 0.393, "trueVerticalFt": 199.0, "lateralClosureRateKt": -43},
        "atEventTime": {"trueLateralNm": 0.9, "trueVerticalFt": 250.0},
        "aircraft_0": {"callsign": "UNKNOWN", "trackId": "750526", "altitudeInFeet": 875, "speedInKnots": 95},
        "aircraft_1": {"callsign": "UNKNOWN", "trackId": "750495", "altitudeInFeet": 975, "speedInKnots": 83},
        "track0": [
            "WSSS,SG,2026-10-31T03:51:55Z,750526,2.30900,102.24300,875,EXCEL05T",
            "WSSS,SG,2026-10-31T03:52:10Z,750526,2.30800,102.24400,900,EXCEL05T",
        ],
        "track1": [
            "WSSS,SG,2026-10-31T03:51:55Z,750495,2.29300,102.23800,975,XL03",
            "WSSS,SG,2026-10-31T03:52:10Z,750495,2.29400,102.23900,950,XL03",
        ],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# CSV out
# --------------------------------------------------------------------------- #

def test_csv_line_is_the_format_openaria_reads():
    line = openaria.csv_line(
        {
            "icao24": "750526",
            "observed_at": __import__("datetime").datetime(2026, 10, 31, 3, 51, 55, tzinfo=__import__("datetime").timezone.utc),
            "lat": 2.309,
            "lon": 102.243,
            "altitude_ft": 875.4,
            "callsign": " EXCEL05T ",
        }
    )
    assert line == "SG,0,2026-10-31T03:51:55Z,750526,2.30900,102.24300,875,EXCEL05T"


def test_csv_line_refuses_a_position_without_altitude():
    """An aircraft with no altitude is not a fix we can hand to a separation engine."""
    with pytest.raises(ValueError):
        openaria.csv_line({"icao24": "abc", "observed_at": "2026-01-01T00:00:00Z", "lat": 1.0, "lon": 103.0})


# --------------------------------------------------------------------------- #
# JSON in
# --------------------------------------------------------------------------- #

def test_iter_events_reads_concatenated_objects(tmp_path):
    """OpenARIA can emit the same event twice into one file; json.load chokes on that."""
    target = tmp_path / "event.json"
    target.write_text(json.dumps(payload()) + "\n" + json.dumps(payload()), encoding="utf-8")
    assert len(list(openaria.iter_events(target))) == 2


def test_load_events_dedupes_by_unique_id(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(payload(eventScore=4.0)), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(payload(eventScore=9.5)), encoding="utf-8")
    events = openaria.load_events(tmp_path)
    assert len(events) == 1
    assert events[0]["eventScore"] == 9.5, "the highest-scoring copy should win"


# --------------------------------------------------------------------------- #
# translation
# --------------------------------------------------------------------------- #

def test_external_ref_is_stable():
    assert openaria.external_ref("abc") == "openaria:abc"
    assert openaria.external_ref("abc") == openaria.external_ref("abc")


@pytest.mark.parametrize(
    "lateral,vertical,expected",
    [
        (0.39, 199, "high"),      # inside 3 NM and 1,000 ft
        (3.0, 1000, "high"),      # exactly on the minima
        (3.5, 400, "medium"),     # inside the wider 5 NM ring
        (7.0, 1200, "medium"),    # vertical inside 1,500 ft
        (7.0, 2000, "low"),       # outside both rings
    ],
)
def test_severity_uses_our_minima_not_openarias_score(lateral, vertical, expected):
    assert openaria.severity_for(lateral, vertical) == expected


def test_event_recovers_identities_from_the_echoed_track_lines():
    """OpenARIA hashes vehicle ids; the raw CSV line it echoes back does not."""
    event = openaria.event_from_openaria(payload())
    assert event["icao24"] == ["750526", "750495"]
    assert event["callsigns"] == ["EXCEL05T", "XL03"]


def test_event_carries_the_arithmetic_that_produced_it():
    event = openaria.event_from_openaria(payload())
    assert event["metrics"]["lateral_nm"] == 0.393
    assert event["metrics"]["vertical_ft"] == 199
    assert event["metrics"]["conflict_angle"] == "CROSSING"
    assert "0.393 NM" in event["provenance"]
    assert "sampling error" in event["provenance"], "sparse ADS-B must not read as a measurement"


def test_event_is_a_loss_of_separation_at_the_aircrafts_altitude():
    event = openaria.event_from_openaria(payload())
    assert event["event_type"] == "loss_of_separation"
    assert event["altitude_ft"] == 875
    assert event["place_icao"] is None, "without an aerodrome reference it must not guess one"


def test_event_names_the_nearest_aerodrome_when_one_is_in_reach():
    aerodromes = [
        {"icao": "WSSS", "name": "Singapore Changi Airport", "lat": 1.3502, "lon": 103.9940},
        {"icao": "WMKK", "name": "Kuala Lumpur International Airport", "lat": 2.7456, "lon": 101.7099},
    ]
    event = openaria.event_from_openaria(payload(latitude=1.3501, longitude=103.9950), aerodromes)
    assert event["place_icao"] == "WSSS"
    assert event["place_name"] == "Singapore Changi Airport"


def test_event_survives_the_shared_validation_door():
    row = store.normalise_event(openaria.event_from_openaria(payload()))
    assert row["source"] == "derived"
    assert row["external_ref"].startswith("openaria:")
    assert row["icao24"] == ["750526", "750495"]


@pytest.mark.parametrize("broken", [{"latitude": None}, {"longitude": 999.0}, {"eventEpochMsTime": None}])
def test_unusable_payloads_are_dropped_rather_than_half_written(broken):
    assert openaria.event_from_openaria(payload(**broken)) is None


def test_missing_callsign_does_not_become_the_word_unknown():
    """'UNKNOWN' in a callsign field is data we do not have, not a name."""
    event = openaria.event_from_openaria(
        payload(track0=["WSSS,SG,2026-10-31T03:51:55Z,750526,2.309,102.243,875,"], track1=[""])
    )
    assert event["callsigns"] == []
    assert event["icao24"] == ["750526", "750495"]


def test_events_from_run_skips_payloads_it_cannot_use(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps(payload()), encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps(payload(uniqueId="bad", latitude=None)), encoding="utf-8")
    assert len(openaria.events_from_run(tmp_path)) == 1


def test_the_batch_pass_never_recreates_the_store():
    """db/001_safety_event.sql begins with DROP TABLE.

    A producer that calls apply_schema erases every event already on the map — which is
    exactly what happened once. The damage is silent: the run looks completely successful.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(openaria))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "apply_schema" not in calls, "the OpenARIA pass must not (re)create the event store"
    assert "_store_is_ready" in inspect.getsource(openaria), "it should check the table exists instead"


# --------------------------------------------------------------------------- #
# the encounter the map draws
# --------------------------------------------------------------------------- #

def test_encounter_tracks_turn_the_echoed_lines_back_into_positions():
    """The map draws exactly the data OpenARIA walked, not a fresh look at the sky."""
    encounter = openaria.encounter_tracks(payload())
    assert encounter is not None
    assert [t["callsign"] for t in encounter["tracks"]] == ["EXCEL05T", "XL03"]
    assert encounter["tracks"][0]["points"][0] == [102.243, 2.309, 875.0], "lon, lat, altitude"
    assert encounter["cpa"]["lateral_nm"] == 0.393


def test_encounter_needs_two_drawable_tracks():
    """Half an encounter is not evidence; better to draw nothing."""
    assert openaria.encounter_tracks(payload(track1=[""])) is None
    assert openaria.encounter_tracks({"track0": [], "track1": []}) is None


def test_encounter_ignores_unparseable_lines():
    encounter = openaria.encounter_tracks(
        payload(
            track0=[
                "WSSS,SG,2026-10-31T03:51:55Z,750526,2.30900,102.24300,875,EXCEL05T",
                "not,a,position",
                "WSSS,SG,2026-10-31T03:52:10Z,750526,2.30800,102.24400,900,EXCEL05T",
            ]
        )
    )
    assert encounter is not None
    assert encounter["tracks"][0]["count"] == 2
