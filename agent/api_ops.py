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
             WHERE event_time > now() - %s AND invalidated_at IS NULL
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
        cur.execute(
            "SELECT COUNT(*) FROM safety_event WHERE event_time > now() - %s AND invalidated_at IS NULL",
            (window,),
        )
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM safety_event "
            "WHERE event_time > now() - make_interval(hours => 1) AND invalidated_at IS NULL"
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
               AND invalidated_at IS NULL
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
               AND invalidated_at IS NULL
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
                   callsigns, metrics, source, detector
              FROM safety_event
             WHERE invalidated_at IS NULL
             ORDER BY event_time DESC, id DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    out = []
    for (
        event_id,
        event_time,
        event_type,
        severity,
        place_icao,
        place_name,
        callsigns,
        metrics,
        source,
        detector,
    ) in rows:
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
                "detector": detector,
                "separation_nm": metrics.get("separation_nm"),
                "vertical_ft": metrics.get("vertical_ft"),
            }
        )
    return out


def detector_yield(conn: Any, hours: int) -> list[dict[str, Any]]:
    """What each detector produced, and how much of it survived re-judgement.

    A snapshot sees one moment and cannot tell a rollout from a fly-by; a trajectory
    engine sees the shape of the whole encounter. Keeping their counts apart is the only
    way to know whether the second is worth its runtime — so the figures here are yields
    and validation rates, not a single blended number.

    Invalidated rows are counted but not deleted, which is what makes the rate computable
    at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(detector, 'unknown') AS detector,
                   COUNT(*) AS produced,
                   COUNT(*) FILTER (WHERE invalidated_at IS NOT NULL) AS invalidated,
                   MAX(event_time) AS last_seen
              FROM safety_event
             WHERE event_time > now() - make_interval(hours => %s)
             GROUP BY 1
             ORDER BY 2 DESC, 1
            """,
            (hours,),
        )
        rows = cur.fetchall()

    out = []
    for detector, produced, invalidated, last_seen in rows:
        kept = produced - invalidated
        out.append(
            {
                "detector": detector,
                "produced": produced,
                "invalidated": invalidated,
                "kept": kept,
                "validation_rate": round(kept / produced, 3) if produced else None,
                "last_seen": _iso(last_seen) if last_seen else None,
            }
        )
    return out


def coverage(conn: Any, minutes: int = 30) -> dict[str, Any]:
    """How much of the airspace we could actually reconstruct, and how well.

    A detection count without this is a claim about the sky; with it, it is a claim about
    what the receivers saw. The interval matters most: this store samples roughly every
    20 seconds, so an interpolated closest approach finer than the distance travelled in
    one interval is arithmetic, not observation.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH obs AS (
                SELECT icao24, observed_at,
                       EXTRACT(EPOCH FROM observed_at
                               - LAG(observed_at) OVER (PARTITION BY icao24 ORDER BY observed_at)) AS gap_s
                  FROM aircraft_track
                 WHERE observed_at > now() - make_interval(mins => %s)
            ), per_aircraft AS (
                SELECT icao24,
                       COUNT(*) AS samples,
                       MAX(gap_s) AS max_gap,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_s) AS median_gap
                  FROM obs
                 GROUP BY 1
            )
            SELECT COUNT(*) AS aircraft,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY samples) AS median_samples,
                   COUNT(*) FILTER (WHERE samples < 5) AS insufficient,
                   COUNT(*) FILTER (WHERE max_gap > 120) AS with_long_gap,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY median_gap) AS median_interval,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY median_gap) AS p95_interval,
                   MAX(max_gap) AS longest_gap
              FROM per_aircraft
            """,
            (minutes,),
        )
        row = cur.fetchone()

    aircraft, median_samples, insufficient, with_long_gap, median_interval, p95_interval, longest_gap = row
    return {
        "window_minutes": minutes,
        "aircraft": aircraft or 0,
        "median_samples": _num(median_samples),
        "insufficient_track": insufficient or 0,
        "with_long_gap": with_long_gap or 0,
        "median_interval_s": _num(median_interval),
        "p95_interval_s": _num(p95_interval),
        "longest_gap_s": _num(longest_gap),
    }


def phase_mix(conn: Any) -> list[dict[str, Any]]:
    """The current airspace, by phase — what the classifier makes of the traffic right now.

    Counted from each aircraft's latest position, so one aircraft is one aircraft. The
    transition count is the part a flat altitude floor used to hide.
    """
    import phase as phase_module  # noqa: PLC0415 — local keeps the module import-light

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (icao24)
                   icao24, altitude_ft, ground_speed_kt, vertical_rate_fpm, on_ground
              FROM aircraft_track
             WHERE observed_at > now() - make_interval(mins => 30)
             ORDER BY icao24, observed_at DESC
            """
        )
        rows = cur.fetchall()

    counts: dict[str, int] = {}
    for _icao, altitude, speed, vertical, on_ground in rows:
        label = phase_module.classify(
            {
                "altitude_ft": altitude,
                "ground_speed_kt": speed,
                "vertical_rate_fpm": vertical,
                "on_ground": bool(on_ground),
            }
        )
        counts[label] = counts.get(label, 0) + 1

    order = ["airborne", "transition", "ground"]
    return [{"phase": name, "aircraft": counts.get(name, 0)} for name in order]


def agreement(conn: Any, hours: int, cluster_seconds: int = 180) -> dict[str, Any]:
    """Where two detectors see the same encounter, and where only one of them does.

    Detection rates are not accuracy: a detector that fires on everything has a high rate
    and no value. The only thing worth comparing is the *same encounter* seen by both —
    and the two lists of encounters only one of them saw, which is the entire question.

    Encounters are matched on the aircraft pair and time (within a few minutes), because
    the two detectors choose their own event timestamps: the snapshot fires at the moment
    the positions were compared, the engine at the computed closest approach.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_time, icao24, callsigns, detector,
                   metrics->>'separation_nm', metrics->>'vertical_ft'
              FROM safety_event
             WHERE event_type = 'loss_of_separation'
               AND invalidated_at IS NULL
               AND event_time > now() - make_interval(hours => %s)
             ORDER BY event_time
            """,
            (hours,),
        )
        rows = cur.fetchall()

    clusters: list[dict[str, Any]] = []
    for event_id, when, icao24, callsigns, detector, separation, vertical in rows:
        pair = tuple(sorted(icao24 or []))
        placed = False
        for cluster in clusters:
            if cluster["pair"] != pair:
                continue
            if abs((when - cluster["last"]).total_seconds()) > cluster_seconds:
                continue
            cluster["last"] = when
            cluster["detectors"].add(detector or "unknown")
            cluster["ids"].append(event_id)
            placed = True
            break
        if not placed:
            clusters.append(
                {
                    "pair": pair,
                    "first": when,
                    "last": when,
                    "detectors": {detector or "unknown"},
                    "ids": [event_id],
                    "callsigns": list(callsigns or []),
                    "separation_nm": separation,
                    "vertical_ft": vertical,
                }
            )

    both, snapshot_only, openaria_only, other = [], [], [], []
    for cluster in clusters:
        seen = cluster["detectors"]
        entry = {
            "ids": cluster["ids"],
            "at": _iso(cluster["first"]),
            "callsigns": cluster["callsigns"] or list(cluster["pair"]),
            "detectors": sorted(seen),
            "separation_nm": cluster["separation_nm"],
            "vertical_ft": cluster["vertical_ft"],
        }
        has_snapshot = "snapshot_separation" in seen
        has_openaria = "openaria" in seen
        if has_snapshot and has_openaria:
            both.append(entry)
        elif has_openaria:
            openaria_only.append(entry)
        elif has_snapshot:
            snapshot_only.append(entry)
        else:
            other.append(entry)

    return {
        "clusters": len(clusters),
        "both": both,
        "snapshot_only": snapshot_only,
        "openaria_only": openaria_only,
        "other": other,
        "note": (
            "Agreement counts the same encounter seen by both detectors, matched on the "
            "aircraft pair within three minutes. A single-detector cluster is not proven "
            "wrong — it is the case worth looking at."
        ),
    }


def _num(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


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
        "detectors": detector_yield(conn, hours),
        "agreement": agreement(conn, hours),
        "coverage": coverage(conn),
        "phases": phase_mix(conn),
        "track_hours": overlap_hours(conn, hours),
    }
    board["took_ms"] = int((time.time() - started) * 1000)
    return board


@router.get("/overview")
def get_overview(hours: int = Query(DEFAULT_HOURS, ge=1, le=168)) -> dict[str, Any]:
    """The whole board: traffic, detections, where they are, and what is flying."""
    with store.connect() as conn:
        return overview(conn, hours=hours)
