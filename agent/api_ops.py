"""The operations board: what is being detected, where, and in what.

The chat answers one question at a time. This answers the standing one — *what is
happening right now* — so nobody has to ask it. Everything here is counted from the
store and the track table; nothing is estimated, and an empty figure is shown as empty
rather than filled in with a plausible number.

One endpoint returns the whole board, because a dashboard that fires eight requests to
draw eight panels goes stale eight different ways.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

import store

router = APIRouter(prefix="/api/ops", tags=["ops"])

# How far back each panel looks. Traffic is a "right now" number; detections are a
# shift; the series is the day.
TRAFFIC_WINDOW_MINUTES = 30
TYPE_WINDOW_MINUTES = 120
DEFAULT_HOURS = 24
RECENT_LIMIT = 12
TOP_N = 6


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def traffic(conn: Any) -> dict[str, Any]:
    """Aircraft seen in the last half hour, split by whether they are flying."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH recent AS (
                SELECT DISTINCT ON (t.icao24)
                       t.icao24, t.observed_at, t.altitude_ft, t.on_ground, t.callsign,
                       m.aircraft_type
                  FROM aircraft_track t
                  LEFT JOIN aircraft_meta m ON m.icao24 = t.icao24
                 WHERE t.observed_at > now() - make_interval(mins => %s)
                 ORDER BY t.icao24, t.observed_at DESC
            )
            SELECT COUNT(*) FILTER (WHERE NOT on_ground AND COALESCE(altitude_ft, 99999) > %s) AS airborne,
                   COUNT(*) FILTER (WHERE on_ground OR COALESCE(altitude_ft, 99999) <= %s) AS ground,
                   COUNT(*) AS total,
                   MAX(observed_at) AS last_fix,
                   COUNT(*) FILTER (WHERE aircraft_type IS NOT NULL) AS with_type
              FROM recent
            """,
            (TRAFFIC_WINDOW_MINUTES, 100, 100),
        )
        airborne, ground, total, last_fix, with_type = cur.fetchone()

    age = None
    if last_fix is not None:
        age = max(0, int((datetime.now(timezone.utc) - last_fix).total_seconds()))
    return {
        "window_minutes": TRAFFIC_WINDOW_MINUTES,
        "airborne": airborne or 0,
        "ground": ground or 0,
        "tracked": total or 0,
        "with_type": with_type or 0,
        "last_fix_age_seconds": age,
        "last_fix_at": _iso(last_fix) if last_fix else None,
    }


def detection_counts(conn: Any, hours: int) -> dict[str, Any]:
    """How many events, of what kind, at what severity, in the window."""
    window = timedelta(hours=hours)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type, severity, COUNT(*)
              FROM safety_event
             WHERE event_time > now() - %s
             GROUP BY 1, 2
             ORDER BY 3 DESC
            """,
            (window,),
        )
        rows = cur.fetchall()

    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_type_severity: dict[str, dict[str, int]] = {}
    for event_type, severity, count in rows:
        by_type[event_type] = by_type.get(event_type, 0) + count
        by_severity[severity] = by_severity.get(severity, 0) + count
        by_type_severity.setdefault(event_type, {})[severity] = count

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM safety_event WHERE event_time > now() - %s", (window,))
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM safety_event WHERE event_time > now() - make_interval(hours => 1)"
        )
        last_hour = cur.fetchone()[0]

    return {
        "hours": hours,
        "total": total or 0,
        "last_hour": last_hour or 0,
        "by_type": [
            {"type": name, "count": count, "severity": by_type_severity.get(name, {})}
            for name, count in sorted(by_type.items(), key=lambda item: -item[1])
        ],
        "by_severity": dict(sorted(by_severity.items(), key=lambda item: -item[1])),
    }


def hourly_series(conn: Any, hours: int) -> list[dict[str, Any]]:
    """Events per hour, including the hours with none — a gap is information."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date_trunc('hour', event_time) AS bucket, COUNT(*)
              FROM safety_event
             WHERE event_time > now() - make_interval(hours => %s)
             GROUP BY 1
             ORDER BY 1
            """,
            (hours,),
        )
        found = {row[0]: row[1] for row in cur.fetchall()}

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours - 1)).replace(minute=0, second=0, microsecond=0)
    series = []
    for step in range(hours):
        bucket = start + timedelta(hours=step)
        series.append({"at": _iso(bucket), "count": found.get(bucket, 0)})
    return series


def places(conn: Any, hours: int, limit: int = TOP_N) -> list[dict[str, Any]]:
    """Where the detections are. Unplaced events are counted, not dropped."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT place_icao, MAX(place_name), COUNT(*) AS count
              FROM safety_event
             WHERE event_time > now() - make_interval(hours => %s)
             GROUP BY place_icao
             ORDER BY count DESC
             LIMIT %s
            """,
            (hours, limit),
        )
        rows = cur.fetchall()
    return [
        {"icao": icao, "name": name, "count": count}
        for icao, name, count in rows
    ]


def aircraft_types(conn: Any, limit: int = TOP_N) -> list[dict[str, Any]]:
    """What is flying, by ICAO type designator.

    Type comes only from the community feed, so this counts the aircraft whose type we
    actually know — the `with_type` figure in `traffic` says how many that is, rather
    than letting the panel look complete.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (t.icao24)
                       t.icao24, m.aircraft_type, t.on_ground, t.altitude_ft
                  FROM aircraft_track t
                  JOIN aircraft_meta m ON m.icao24 = t.icao24
                 WHERE t.observed_at > now() - make_interval(mins => %s)
                   AND m.aircraft_type IS NOT NULL
                 ORDER BY t.icao24, t.observed_at DESC
            )
            SELECT aircraft_type,
                   COUNT(*) AS aircraft,
                   COUNT(*) FILTER (WHERE NOT on_ground AND COALESCE(altitude_ft, 99999) > 100) AS airborne
              FROM latest
             GROUP BY 1
             ORDER BY 2 DESC, 1
             LIMIT %s
            """,
            (TYPE_WINDOW_MINUTES, limit),
        )
        rows = cur.fetchall()
    return [
        {"type": type_code, "aircraft": aircraft, "airborne": airborne}
        for type_code, aircraft, airborne in rows
    ]


def recent(conn: Any, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    """The newest detections, shaped the way the board lists them."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_time, event_type, severity, place_icao, place_name,
                   callsigns, metrics, source
              FROM safety_event
             ORDER BY event_time DESC, id DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    out = []
    for event_id, event_time, event_type, severity, place_icao, place_name, callsigns, metrics, source in rows:
        metrics = metrics or {}
        out.append(
            {
                "id": event_id,
                "event_time": _iso(event_time),
                "event_type": event_type,
                "severity": severity,
                "place_icao": place_icao,
                "place_name": place_name,
                "callsigns": list(callsigns or []),
                "source": source,
                "detector": metrics.get("detector") or metrics.get("engine"),
                "separation_nm": metrics.get("separation_nm"),
                "vertical_ft": metrics.get("vertical_ft"),
            }
        )
    return out


def overlap_hours(conn: Any, hours: int) -> int:
    """Hours of stored track data behind the board — how much history it can speak for."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM (MAX(observed_at) - MIN(observed_at)))::float8 / 3600.0
              FROM aircraft_track
             WHERE observed_at > now() - make_interval(hours => %s)
            """,
            (hours,),
        )
        span = cur.fetchone()[0]
    return max(0, int(round(span or 0)))


def overview(conn: Any, hours: int = DEFAULT_HOURS) -> dict[str, Any]:
    """Everything the board draws, from one connection and one moment."""
    started = time.time()
    board = {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "traffic": traffic(conn),
        "detections": detection_counts(conn, hours),
        "series": hourly_series(conn, hours),
        "places": places(conn, hours),
        "types": aircraft_types(conn),
        "recent": recent(conn),
        "track_hours": overlap_hours(conn, hours),
    }
    board["took_ms"] = int((time.time() - started) * 1000)
    return board


@router.get("/overview")
def get_overview(hours: int = Query(DEFAULT_HOURS, ge=1, le=168)) -> dict[str, Any]:
    """The whole board: traffic, detections, where they are, and what is flying."""
    with store.connect() as conn:
        return overview(conn, hours=hours)
