"""Tests for the operations board.

The board is the one screen nobody asks a question about, so a wrong number there is
never caught by a human cross-checking a sentence. These tests pin the two ways it could
lie: a figure invented when the data is absent, and a gap in the day drawn as if it were
a measurement.
"""
from datetime import datetime, timedelta, timezone

import pytest

import api_ops


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.owner.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.owner.one.pop(0) if self.owner.one else (0,)

    def fetchall(self):
        return self.owner.many.pop(0) if self.owner.many else []


class FakeConn:
    def __init__(self, one=None, many=None):
        self.statements = []
        self.one = list(one or [])
        self.many = list(many or [])

    def cursor(self):
        return FakeCursor(self)


# --------------------------------------------------------------------------- #
# what the panels say when there is nothing
# --------------------------------------------------------------------------- #


def test_empty_traffic_reports_zero_rather_than_none():
    board = api_ops.traffic(FakeConn(one=[(None, None, None, None, None)]))
    assert board["airborne"] == 0
    assert board["ground"] == 0
    assert board["tracked"] == 0
    assert board["with_type"] == 0
    assert board["last_fix_age_seconds"] is None
    assert board["last_fix_at"] is None


def test_last_fix_age_is_seconds_since_the_freshest_position():
    fixed = datetime.now(timezone.utc) - timedelta(seconds=42)
    board = api_ops.traffic(FakeConn(one=[(70, 24, 94, fixed, 6)]))
    assert board["airborne"] == 70
    assert board["ground"] == 24
    assert board["with_type"] == 6
    assert 41 <= board["last_fix_age_seconds"] <= 44


def test_an_hour_with_no_detections_is_drawn_as_zero():
    """The series must cover every hour in the window, not only the ones with events."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    busy = now - timedelta(hours=3)
    series = api_ops.hourly_series(FakeConn(many=[[(busy, 5)]]), hours=6)

    assert len(series) == 6
    counts = [point["count"] for point in series]
    assert counts.count(0) == 5
    assert sum(counts) == 5
    assert series[-1]["at"].endswith("Z")


def test_detections_are_grouped_by_type_and_severity_together():
    conn = FakeConn(
        one=[(65,), (4,)],
        many=[[
            ("loss_of_separation", "high", 40),
            ("unstable_approach", "medium", 16),
            ("go_around", "low", 9),
        ]],
    )
    board = api_ops.detection_counts(conn, hours=24)
    assert board["total"] == 65
    assert board["last_hour"] == 4
    assert [item["type"] for item in board["by_type"]] == [
        "loss_of_separation",
        "unstable_approach",
        "go_around",
    ]
    assert board["by_severity"] == {"high": 40, "medium": 16, "low": 9}
    assert board["by_type"][0]["severity"]["high"] == 40


def test_unplaced_detections_are_counted_not_hidden():
    places = api_ops.places(FakeConn(many=[[("WSSS", "Singapore Changi Airport", 28), (None, None, 12)]]), hours=24)
    assert places[0] == {"icao": "WSSS", "name": "Singapore Changi Airport", "count": 28}
    assert places[1]["icao"] is None
    assert places[1]["count"] == 12


def test_recent_events_survive_a_missing_metrics_object():
    """Every producer does not write every metric; a null must not break the list."""
    row = (
        231,
        datetime.now(timezone.utc),
        "loss_of_separation",
        "high",
        "WSSS",
        "Singapore Changi Airport",
        ["AXM718", "MMA233"],
        None,
        "derived",
        "snapshot_separation",
    )
    events = api_ops.recent(FakeConn(many=[[row]]))
    assert events[0]["separation_nm"] is None
    assert events[0]["detector"] == "snapshot_separation"
    assert events[0]["callsigns"] == ["AXM718", "MMA233"]


# --------------------------------------------------------------------------- #
# the board as a whole
# --------------------------------------------------------------------------- #


def test_the_board_is_one_round_trip(monkeypatch):
    """Eight panels, one query pass: a dashboard that fetches eight ways goes stale
    eight different ways."""
    opened = {"count": 0}
    conn = FakeConn(
        one=[(74, 20, 94, datetime.now(timezone.utc), 6), (65,), (4,), (16.0,)],
        many=[[], [(datetime.now(timezone.utc), 3)], [], [], []],
    )

    def fake_connect():
        opened["count"] += 1
        return _Context(conn)

    monkeypatch.setattr(api_ops.store, "connect", fake_connect)
    board = api_ops.get_overview(hours=24)

    assert opened["count"] == 1
    assert set(board) >= {"traffic", "detections", "series", "places", "types", "recent", "track_hours"}
    assert board["track_hours"] == 16
    assert board["took_ms"] >= 0


class _Context:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def test_the_board_counts_only_active_detections():
    """Invalidated rows stay in the table; the board is the active picture."""
    conn = FakeConn(one=[(65,), (4,)])
    api_ops.detection_counts(conn, hours=24)
    statements = [sql for sql, _ in conn.statements]
    assert any("invalidated_at IS NULL" in sql for sql in statements)


def test_detector_yields_are_reported_with_their_validation_rate():
    """A detector that produces a lot and survives rarely is worth less than its count."""
    from datetime import datetime, timezone

    rows = [
        ("snapshot_separation", 41, 10, datetime.now(timezone.utc)),
        ("openaria", 4, 0, datetime.now(timezone.utc)),
    ]
    yields = api_ops.detector_yield(FakeConn(many=[rows]), hours=24)

    assert yields[0]["detector"] == "snapshot_separation"
    assert yields[0]["produced"] == 41
    assert yields[0]["kept"] == 31
    assert yields[0]["validation_rate"] == 0.756
    assert yields[1]["validation_rate"] == 1.0


def test_a_detector_that_produced_nothing_reports_no_rate_rather_than_zero():
    yields = api_ops.detector_yield(FakeConn(many=[[("openaria", 0, 0, None)]]), hours=24)
    assert yields[0]["validation_rate"] is None
    assert yields[0]["last_seen"] is None


def test_the_window_is_bounded_so_the_board_cannot_be_asked_for_forever():
    """A dashboard asking for a year of events would scan the whole table on every tick."""
    route = next(
        item for item in api_ops.router.routes if getattr(item, "path", "") == "/api/ops/overview"
    )
    limits = {type(item).__name__: item for item in route.dependant.query_params[0].field_info.metadata}
    assert limits["Ge"].ge == 1
    assert limits["Le"].le == 168
