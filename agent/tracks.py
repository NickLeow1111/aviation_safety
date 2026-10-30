"""Recent position history — what turns a snapshot into a track.

One snapshot says where an aircraft is. Only the previous minute says that it is on
final and its altitude has just started *rising* again, which is a go-around; or that
it has turned through 300 degrees inside a small circle, which is a hold.

The store is deliberately in memory: ten minutes of history for the aircraft we have
actually seen, pruned on every write. It costs nothing, needs no migration, and
disappears on restart — which is the honest trade for a first version. Anything
longer than "what just happened" should be persisted, and that is a separate job.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Any, Iterable

log = logging.getLogger("safety-intel-bot")

HISTORY_SECONDS = 600.0
MAX_SAMPLES_PER_AIRCRAFT = 90
GO_AROUND_WINDOW_SECONDS = 240.0
HOLDING_WINDOW_SECONDS = 300.0
HOLDING_CUMULATIVE_TURN_DEG = 300.0
HOLDING_MAX_RADIUS_NM = 4.0

_store: dict[str, deque[dict[str, Any]]] = {}


def reset() -> None:
    _store.clear()


def record(aircraft: Iterable[dict[str, Any]], now: float | None = None) -> int:
    """Append one sample per aircraft. Returns how many were stored."""
    moment = time.time() if now is None else now
    stored = 0
    for item in aircraft:
        icao24 = str(item.get("icao24") or "")
        lat, lon = item.get("lat"), item.get("lon")
        if not icao24 or lat is None or lon is None:
            continue
        samples = _store.setdefault(icao24, deque(maxlen=MAX_SAMPLES_PER_AIRCRAFT))
        samples.append(
            {
                "t": moment,
                "lat": float(lat),
                "lon": float(lon),
                "altitude_ft": item.get("altitude_ft"),
                "ground_speed_kt": item.get("ground_speed_kt"),
                "track_deg": item.get("track_deg"),
                "vertical_rate_fpm": item.get("vertical_rate_fpm"),
                "on_ground": bool(item.get("on_ground")),
                "callsign": item.get("callsign"),
            }
        )
        stored += 1
    _prune(moment)
    return stored


def history(icao24: str) -> list[dict[str, Any]]:
    return list(_store.get(icao24, ()))


def tracked_count() -> int:
    return len(_store)


def _prune(now: float) -> None:
    cutoff = now - HISTORY_SECONDS
    for icao24 in list(_store):
        samples = _store[icao24]
        while samples and samples[0]["t"] < cutoff:
            samples.popleft()
        if not samples:
            _store.pop(icao24, None)


# --------------------------------------------------------------------------- #
# motion events
# --------------------------------------------------------------------------- #

def _nm_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance in nautical miles. Fine at the range of a local view."""
    mean_lat = (lat1 + lat2) / 2.0
    dy = (lat2 - lat1) * 60.0
    dx = (lon2 - lon1) * 60.0 * max(0.05, abs(math.cos(math.radians(mean_lat))))
    return math.hypot(dx, dy)


def _turn_delta(previous: float, current: float) -> float:
    return (current - previous + 180.0) % 360.0 - 180.0


def _go_around(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Descended towards the runway and then climbed away again below 3,000 ft."""
    recent = [s for s in samples if s["altitude_ft"] is not None and s["vertical_rate_fpm"] is not None]
    if len(recent) < 3:
        return None

    lowest_down = None
    for index, sample in enumerate(recent):
        if sample["altitude_ft"] < 2000 and sample["vertical_rate_fpm"] < -300:
            lowest_down = (index, sample)
            break
    if lowest_down is None:
        return None

    for sample in recent[lowest_down[0] + 1:]:
        if sample["vertical_rate_fpm"] > 400 and sample["altitude_ft"] < 3500:
            climb = sample["vertical_rate_fpm"]
            return {
                "kind": "go_around",
                "callsign": sample.get("callsign"),
                "detail": (
                    f"descended to {lowest_down[1]['altitude_ft']:.0f} ft, now climbing "
                    f"at {climb:.0f} ft/min through {sample['altitude_ft']:.0f} ft"
                ),
                "lat": sample["lat"],
                "lon": sample["lon"],
                "altitude_ft": sample["altitude_ft"],
            }
    return None


def _holding(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Turned through most of a circle without going anywhere: a hold or an orbit."""
    airborne = [s for s in samples if not s["on_ground"] and s["track_deg"] is not None]
    if len(airborne) < 4:
        return None

    cumulative = 0.0
    for previous, current in zip(airborne, airborne[1:]):
        cumulative += abs(_turn_delta(previous["track_deg"], current["track_deg"]))
        if cumulative < HOLDING_CUMULATIVE_TURN_DEG:
            continue
        window = [s for s in airborne if s["t"] >= previous["t"] - HOLDING_WINDOW_SECONDS]
        if len(window) < 4:
            continue
        lats = [s["lat"] for s in window]
        lons = [s["lon"] for s in window]
        centre_lat = sum(lats) / len(lats)
        centre_lon = sum(lons) / len(lons)
        radius = max(_nm_between(centre_lat, centre_lon, s["lat"], s["lon"]) for s in window)
        if radius > HOLDING_MAX_RADIUS_NM:
            continue
        altitude = current["altitude_ft"]
        return {
            "kind": "holding_pattern",
            "callsign": current.get("callsign"),
            "detail": (
                f"turned {cumulative:.0f}° within {radius:.1f} NM"
                + (f" at {altitude:.0f} ft" if altitude is not None else "")
            ),
            "lat": current["lat"],
            "lon": current["lon"],
            "altitude_ft": altitude,
        }
    return None


def _unstable_approach(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Low on final but too fast, or dropping far too steeply to settle."""
    current = samples[-1]
    altitude = current["altitude_ft"]
    if altitude is None or altitude > 1200 or current["on_ground"]:
        return None
    speed = current["ground_speed_kt"]
    vertical = current["vertical_rate_fpm"]
    if speed is not None and speed > 190:
        return {
            "kind": "unstable_approach",
            "callsign": current.get("callsign"),
            "detail": f"{speed:.0f} kt at {altitude:.0f} ft on approach",
            "lat": current["lat"],
            "lon": current["lon"],
            "altitude_ft": altitude,
        }
    if vertical is not None and vertical < -1500:
        return {
            "kind": "unstable_approach",
            "callsign": current.get("callsign"),
            "detail": f"descending {abs(vertical):.0f} ft/min at {altitude:.0f} ft",
            "lat": current["lat"],
            "lon": current["lon"],
            "altitude_ft": altitude,
        }
    return None


def motion_events() -> dict[str, Any]:
    """Every aircraft's track, judged on how it has been moving."""
    events: list[dict[str, Any]] = []
    for icao24, samples in _store.items():
        series = list(samples)
        if len(series) < 3:
            continue
        for detector in (_go_around, _holding, _unstable_approach):
            found = detector(series)
            if found:
                found["icao24"] = icao24
                events.append(found)

    return {
        "events": events,
        "aircraft_with_history": len(_store),
        "summary": (
            "No manoeuvre looks unusual in the tracks seen so far."
            if not events
            else f"{len(events)} track event(s): " + ", ".join(sorted({e['kind'] for e in events}))
        ),
        "disclaimer": (
            "Read from the last few minutes of position reports. A go-around is a normal, "
            "safe manoeuvre — it is reported because it is worth knowing about, not because "
            "anything went wrong."
        ),
    }


# --------------------------------------------------------------------------- #
# persistence
#
# The in-memory store answers "what is happening now". These functions answer
# "what happened this week", which is the question that makes a detector auditable:
# a rule nobody can review against history is a rule nobody can trust.
# --------------------------------------------------------------------------- #

RETENTION_DAYS = 7
EVENT_DEDUPE_SECONDS = 900

_schema_ready = False
_last_prune = 0.0
_warned = False


def _log_once(message: str, exc: BaseException) -> None:
    global _warned
    if not _warned:
        log.warning("%s: %s: %s", message, type(exc).__name__, exc)
        _warned = True


def ensure_schema(conn: Any) -> None:
    """Create the history tables once per process. Safe to call every poll."""
    global _schema_ready
    if _schema_ready:
        return
    from pathlib import Path as _Path

    schema = _Path(__file__).resolve().parents[1] / "db" / "003_history.sql"
    try:
        with conn.cursor() as cur:
            cur.execute(schema.read_text(encoding="utf-8"))
        _schema_ready = True
    except Exception as exc:  # noqa: BLE001 - history is an enhancement, not a dependency
        _log_once("track history unavailable", exc)
        conn.rollback()


def warm(conn: Any, now: float | None = None) -> int:
    """Fill an empty store from the database, so a restart does not blind the checks."""
    if _store:
        return 0
    moment = time.time() if now is None else now
    cutoff = moment - HISTORY_SECONDS
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT icao24, lat, lon, altitude_ft, ground_speed_kt, track_deg,
                          vertical_rate_fpm, on_ground, callsign,
                          EXTRACT(EPOCH FROM observed_at)::float8
                     FROM aircraft_track
                    WHERE observed_at > now() - make_interval(secs => %s)
                    ORDER BY observed_at ASC""",
                (HISTORY_SECONDS,),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        _log_once("could not warm track history", exc)
        conn.rollback()
        return 0

    loaded = 0
    for icao24, lat, lon, alt, speed, track, vertical, on_ground, callsign, epoch in rows:
        samples = _store.setdefault(icao24, deque(maxlen=MAX_SAMPLES_PER_AIRCRAFT))
        samples.append(
            {
                "t": float(epoch),
                "lat": float(lat),
                "lon": float(lon),
                "altitude_ft": alt,
                "ground_speed_kt": speed,
                "track_deg": track,
                "vertical_rate_fpm": vertical,
                "on_ground": bool(on_ground),
                "callsign": callsign,
            }
        )
        loaded += 1
    _prune(moment)
    return loaded


def persist(conn: Any, aircraft: Iterable[dict[str, Any]], now: float | None = None) -> int:
    """Write one poll's positions. Returns rows written."""
    moment = time.time() if now is None else now
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
                    track_deg, vertical_rate_fpm, on_ground, callsign)
                   VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)""",
                rows,
            )
            _maybe_prune(cur, moment)
    except Exception as exc:  # noqa: BLE001
        _log_once("could not persist track samples", exc)
        conn.rollback()
        return 0
    return len(rows)


def _maybe_prune(cur: Any, moment: float) -> None:
    """Drop old samples at most once an hour, on this connection, in this call."""
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
    count one hold as forty.
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

    total = sum(by_kind.values())
    return {
        "available": True,
        "days": days,
        "total": total,
        "by_kind": by_kind,
        "top_aircraft": top_aircraft,
        "by_day": by_day,
    }
