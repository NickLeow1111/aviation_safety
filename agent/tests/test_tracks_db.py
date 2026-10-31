"""Tests for the history layer.

The persistence path is thin SQL, so the tests use a recording fake connection
rather than a database: what matters is that a repeated manoeuvre is not counted
forty times, that a broken database never takes the live map down, and that the
assistant is told which numbers are detector output and which are reported events.
"""
import pytest

import tracks
import tracks_db


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.owner.fail_on and self.owner.fail_on in sql:
            raise RuntimeError("database went away")
        self.owner.statements.append((" ".join(sql.split()), params))

    def executemany(self, sql, rows):
        if self.owner.fail_on and self.owner.fail_on in sql:
            raise RuntimeError("database went away")
        self.owner.statements.append((" ".join(sql.split()), rows))

    def fetchone(self):
        return self.owner.one.pop(0) if self.owner.one else None

    def fetchall(self):
        return self.owner.many.pop(0) if self.owner.many else []


class FakeConn:
    def __init__(self, fail_on=None, one=None, many=None):
        self.statements = []
        self.fail_on = fail_on
        self.one = list(one or [])
        self.many = list(many or [])
        self.rolled_back = 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back += 1

    def executed(self, needle):
        return [s for s in self.statements if needle in s[0]]


@pytest.fixture(autouse=True)
def clean():
    tracks.reset()
    tracks_db.reset()
    yield
    tracks.reset()
    tracks_db.reset()


def board_event(**overrides):
    base = {
        "icao24": "abc123",
        "callsign": "SIA1",
        "kind": "go_around",
        "detail": "descended to 900 ft, now climbing",
        "lat": 1.35,
        "lon": 103.99,
        "altitude_ft": 900,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def test_positions_are_written_for_aircraft_that_have_one():
    conn = FakeConn()
    rows = tracks_db.persist(
        conn,
        [
            {"icao24": "a", "lat": 1.3, "lon": 103.9, "altitude_ft": 1000, "callsign": "X"},
            {"icao24": "b", "lat": None, "lon": None},  # no position: skip
            {"icao24": "", "lat": 1.4, "lon": 104.0},  # no identity: skip
        ],
        now=1000.0,
    )
    assert rows == 1
    inserted = conn.executed("INSERT INTO aircraft_track")
    assert len(inserted) == 1
    assert len(inserted[0][1]) == 1


def test_nothing_to_write_is_not_an_error():
    conn = FakeConn()
    assert tracks_db.persist(conn, []) == 0
    assert conn.statements == []


def test_the_aircraft_type_is_remembered_per_airframe_not_per_position():
    """Only the community feed reports a type, and only on some of its fixes.

    Merged polls hand back OpenSky rows for the same aircraft, so a per-position column
    would keep erasing what we know — the meta upsert is what makes the type panel
    possible.
    """
    conn = FakeConn()
    tracks_db.persist(
        conn,
        [
            {"icao24": "76CE87", "lat": 1.3, "lon": 103.9, "altitude_ft": 30000, "aircraft_type": "B38M"},
            {"icao24": "76CD88", "lat": 1.4, "lon": 104.0, "altitude_ft": 12000, "callsign": "SIA1"},
        ],
        now=1000.0,
    )

    meta = conn.executed("INSERT INTO aircraft_meta")
    assert len(meta) == 1
    sql, rows = meta[0]
    # The blank fix must not overwrite a remembered type.
    assert "COALESCE(EXCLUDED.aircraft_type, aircraft_meta.aircraft_type)" in sql
    assert ("76ce87", "B38M", None, 1000.0, 1000.0) in rows
    assert ("76cd88", None, "SIA1", 1000.0, 1000.0) in rows


def test_a_failing_meta_upsert_does_not_cost_the_positions():
    conn = FakeConn(fail_on="aircraft_meta")
    rows = tracks_db.persist(
        conn,
        [{"icao24": "a", "lat": 1.3, "lon": 103.9, "altitude_ft": 1000}],
        now=1000.0,
    )
    assert rows == 0
    assert conn.rolled_back == 1


def test_old_samples_are_pruned_at_most_hourly():
    conn = FakeConn()
    tracks_db.persist(conn, [{"icao24": "a", "lat": 1.3, "lon": 103.9}], now=1000.0)
    tracks_db.persist(conn, [{"icao24": "a", "lat": 1.3, "lon": 103.9}], now=1010.0)
    assert len(conn.executed("DELETE FROM aircraft_track")) == 1
    tracks_db.persist(conn, [{"icao24": "a", "lat": 1.3, "lon": 103.9}], now=4700.0)
    assert len(conn.executed("DELETE FROM aircraft_track")) == 2


def test_a_manoeuvre_is_recorded_once_then_deduplicated():
    conn = FakeConn(one=[None, None])  # nothing seen before -> insert
    assert tracks_db.persist_events(conn, [board_event()], now=1000.0) == 1
    assert len(conn.executed("INSERT INTO track_event")) == 1

    conn = FakeConn(one=[("exists",)])  # same aircraft, same kind, inside the window
    assert tracks_db.persist_events(conn, [board_event()], now=1010.0) == 0
    assert conn.executed("INSERT INTO track_event") == []


def test_database_failure_does_not_raise_and_rolls_back():
    conn = FakeConn(fail_on="INSERT INTO track_event")
    assert tracks_db.persist_events(conn, [board_event()]) == 0
    assert conn.rolled_back == 1


def test_schema_creation_is_attempted_once_per_process():
    conn = FakeConn()
    assert tracks_db.ensure_schema(conn) is True
    assert tracks_db.ensure_schema(conn) is True
    assert len(conn.executed("CREATE TABLE IF NOT EXISTS aircraft_track")) == 1


def test_schema_failure_is_reported_not_raised():
    conn = FakeConn(fail_on="CREATE TABLE")
    assert tracks_db.ensure_schema(conn) is False
    assert conn.rolled_back == 1


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def test_history_summary_reads_three_questions():
    conn = FakeConn(many=[[("go_around", 3)], [("SIA1", 2)], [("2026-10-30", 3)]])
    summary = tracks_db.history_summary(conn, days=7)
    assert summary["available"] is True
    assert summary["total"] == 3
    assert summary["by_kind"] == {"go_around": 3}
    assert summary["top_aircraft"][0]["callsign"] == "SIA1"
    assert summary["days"] == 7


def test_history_summary_is_unavailable_rather_than_empty_on_failure():
    conn = FakeConn(fail_on="SELECT kind")
    assert tracks_db.history_summary(conn) == {"available": False}


def test_warming_a_populated_store_does_nothing():
    tracks.record([{"icao24": "a", "lat": 1.3, "lon": 103.9}])
    conn = FakeConn()
    assert tracks_db.warm(conn) == 0
    assert conn.statements == []


# --------------------------------------------------------------------------- #
# wording shown to the assistant
# --------------------------------------------------------------------------- #

def test_zero_counts_still_name_the_detector():
    text = tracks_db.render_summary({"available": True, "days": 7, "total": 0, "by_kind": {}})
    assert "No manoeuvres recorded" in text
    assert "separate from the reported events" in text


def test_counts_are_attributed_to_the_detector_not_the_event_store():
    text = tracks_db.render_summary(
        {"available": True, "days": 7, "total": 4, "by_kind": {"go_around": 3, "holding_pattern": 1}}
    )
    assert "4 manoeuvre(s)" in text
    assert "go around: 3" in text
    assert "not reported events" in text


def test_missing_history_says_so_rather_than_zero():
    assert "not available" in tracks_db.render_summary({"available": False})
