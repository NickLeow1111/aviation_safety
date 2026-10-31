"""Persistence for live-traffic history.

`tracks.py` answers "what is happening now" from memory. This module answers "what
happened this week", which is the question that makes a detector auditable: a rule
nobody can review against history is a rule nobody can trust.

Both halves hang off two tables (db/003_history.sql). Every function here degrades
to a no-op with a single logged warning if Postgres is missing, because losing
history should never take the live map down with it.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterable

import tracks

log = logging.getLogger("safety-intel-bot")

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "db" / "003_history.sql"
RETENTION_DAYS = 7
EVENT_DEDUPE_SECONDS = 900

_schema_ready = False
_last_prune = float("-inf")
_warned = False


def _log_once(message: str, exc: BaseException) -> None:
    global _warned
    if not _warned:
        log.warning("%s: %s: %s", message, type(exc).__name__, exc)
        _warned = True


def reset() -> None:
    """Tests only: forget that the schema and pruner have run."""
    global _schema_ready, _last_prune, _warned
    _schema_ready = False
    _last_prune = float("-inf")
    _warned = False


def ensure_schema(conn: Any) -> bool:
    """Create the history tables once per process. Safe to call on every poll."""
    global _schema_ready
    if _schema_ready:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        _schema_ready = True
        return True
    except Exception as exc:  # noqa: BLE001 - history is an enhancement, not a dependency
        _log_once("track history unavailable", exc)
        conn.rollback()
        return False


def warm(conn: Any, now: float | None = None) -> int:
    """Fill an empty in-memory store from the database after a restart."""
    if tracks.tracked_count():
        return 0
    moment = time.time() if now is None else now
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT icao24, lat, lon, altitude_ft, ground_speed_kt, track_deg,
                          vertical_rate_fpm, on_ground, callsign,
                          EXTRACT(EPOCH FROM observed_at)::float8
                     FROM aircraft_track
                    WHERE observed_at > now() - make_interval(secs => %s)
                    ORDER BY observed_at ASC""",
                (tracks.HISTORY_SECONDS,),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        _log_once("could not warm track history", exc)
        conn.rollback()
        return 0

    loaded = 0
    for icao24, lat, lon, altitude, speed, track, vertical, on_ground, callsign, epoch in rows:
        samples = tracks._store.setdefault(icao24, tracks.deque(maxlen=tracks.MAX_SAMPLES_PER_AIRCRAFT))
        samples.append(
            {
                "t": float(epoch),
                "lat": float(lat),
                "lon": float(lon),
                "altitude_ft": altitude,
                "ground_speed_kt": speed,
                "track_deg": track,
                "vertical_rate_fpm": vertical,
                "on_ground": bool(on_ground),
                "callsign": callsign,
            }
        )
        loaded += 1
    tracks._prune(moment)
    return loaded


def persist(conn: Any, aircraft: Iterable[dict[str, Any]], now: float | None = None) -> int:
    """Write one poll's positions. Returns the number of rows written."""
    moment = time.time() if now is None else now
    aircraft = list(aircraft)
    rows = [
        (
            str(item["icao24"]),
            moment,
            float(item["lat"]),
            float(item["lon"]),
            item.get("altitude_ft"),
            item.get("ground_speed_kt"),
            item.get("track_deg"),
            item.get("vertical_rate_fpm"),
            bool(item.get("on_ground")),
            item.get("callsign"),
            item.get("aircraft_type"),
        )
        for item in aircraft
        if item.get("icao24") and item.get("lat") is not None and item.get("lon") is not None
    ]
    if not rows:
        return 0

    try:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO aircraft_track
                   (icao24, observed_at, lat, lon, altitude_ft, ground_speed_kt,
                    track_deg, vertical_rate_fpm, on_ground, callsign, aircraft_type)
                   VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                rows,
            )
            _persist_meta(cur, aircraft, moment)
            _maybe_prune(cur, moment)
    except Exception as exc:  # noqa: BLE001
        _log_once("could not persist track samples", exc)
        conn.rollback()
        return 0
    return len(rows)


def _persist_meta(cur: Any, aircraft: list[dict[str, Any]], moment: float) -> None:
    """Remember what each airframe is, without letting a blank fix blank the record.

    Only the community feed reports a type, and only on some of its fixes. Merged polls
    hand back OpenSky rows for the same aircraft, so writing the type per position would
    keep erasing it; COALESCE keeps the last thing we knew.
    """
    rows = [
        (
            str(item["icao24"]).lower(),
            item.get("aircraft_type"),
            (item.get("callsign") or None),
        )
        for item in aircraft
        if item.get("icao24")
    ]
    if not rows:
        return
    cur.executemany(
        """INSERT INTO aircraft_meta (icao24, aircraft_type, callsign, first_seen, last_seen)
           VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s))
           ON CONFLICT (icao24) DO UPDATE
              SET aircraft_type = COALESCE(EXCLUDED.aircraft_type, aircraft_meta.aircraft_type),
                  callsign      = COALESCE(EXCLUDED.callsign, aircraft_meta.callsign),
                  last_seen     = EXCLUDED.last_seen""",
        [(icao24, kind, callsign, moment, moment) for icao24, kind, callsign in rows],
    )


def _maybe_prune(cur: Any, moment: float) -> None:
    global _last_prune
    if moment - _last_prune < 3600:
        return
    cur.execute(
        "DELETE FROM aircraft_track WHERE observed_at < now() - make_interval(days => %s)",
        (RETENTION_DAYS,),
    )
    _last_prune = moment


def persist_events(conn: Any, events: list[dict[str, Any]], now: float | None = None) -> int:
    """Store manoeuvres, ignoring repeats of the same kind for the same aircraft.

    A holding aircraft is re-detected on every poll; without this the table would
    record one hold forty times and the weekly count would be meaningless.
    """
    moment = time.time() if now is None else now
    stored = 0
    try:
        with conn.cursor() as cur:
            for event in events:
                cur.execute(
                    """SELECT 1 FROM track_event
                        WHERE icao24 = %s AND kind = %s
                          AND observed_at > now() - make_interval(secs => %s)
                        LIMIT 1""",
                    (event.get("icao24"), event["kind"], EVENT_DEDUPE_SECONDS),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO track_event
                       (icao24, callsign, kind, detail, lat, lon, altitude_ft, observed_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))""",
                    (
                        event.get("icao24"),
                        event.get("callsign"),
                        event["kind"],
                        event["detail"],
                        event.get("lat"),
                        event.get("lon"),
                        event.get("altitude_ft"),
                        moment,
                    ),
                )
                stored += 1
    except Exception as exc:  # noqa: BLE001
        _log_once("could not persist track events", exc)
        conn.rollback()
        return 0
    return stored


def history_summary(conn: Any, days: int = 7) -> dict[str, Any]:
    """What the detector has seen over the last `days`, for the assistant to quote."""
    days = max(1, min(90, int(days)))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT kind, count(*) FROM track_event
                    WHERE observed_at > now() - make_interval(days => %s)
                    GROUP BY 1 ORDER BY 2 DESC""",
                (days,),
            )
            by_kind = {row[0]: int(row[1]) for row in cur.fetchall()}

            cur.execute(
                """SELECT callsign, count(*) FROM track_event
                    WHERE observed_at > now() - make_interval(days => %s)
                      AND callsign IS NOT NULL
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 5""",
                (days,),
            )
            top_aircraft = [{"callsign": row[0], "count": int(row[1])} for row in cur.fetchall()]

            cur.execute(
                """SELECT to_char(date_trunc('day', observed_at), 'YYYY-MM-DD'), count(*)
                     FROM track_event
                    WHERE observed_at > now() - make_interval(days => %s)
                    GROUP BY 1 ORDER BY 1 DESC LIMIT 10""",
                (days,),
            )
            by_day = [{"day": row[0], "count": int(row[1])} for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        _log_once("could not read track history", exc)
        conn.rollback()
        return {"available": False}

    return {
        "available": True,
        "days": days,
        "total": sum(by_kind.values()),
        "by_kind": by_kind,
        "top_aircraft": top_aircraft,
        "by_day": by_day,
    }


def render_summary(summary: dict[str, Any]) -> str:
    """One line for the assistant's context block."""
    if not summary.get("available"):
        return "Track history is not available in this deployment."
    if not summary.get("total"):
        return (
            f"No manoeuvres recorded by the track detector in the last {summary['days']} days. "
            "This is separate from the reported events in the event database."
        )
    parts = ", ".join(f"{kind.replace('_', ' ')}: {count}" for kind, count in summary["by_kind"].items())
    return (
        f"{summary['total']} manoeuvre(s) recorded by the track detector in the last "
        f"{summary['days']} days ({parts}). These are detector output, not reported events."
    )
