"""The analysis surface: evidence around one event, rates rather than counts, and repetition.

Three questions the operations board cannot answer, because it is built to describe *now*:

- **Is this event real?** — the case file: provenance, the metrics behind the detection, the
  state of the tracks it was derived from, and whether the same pair has done this before.
- **Where should I look first?** — events expressed per aircraft seen, so a busy aerodrome
  stops looking dangerous merely because it is busy.
- **Is this one aircraft or a pattern?** — callsigns and pairs that keep coming back.

Every number here is queried, never inferred. Where a denominator is a proxy — aircraft seen
within 10 NM, not a certified movement count — the field is named `aircraft` and the response
says so, rather than calling it "movements" and implying an accuracy it does not have.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import store

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

_AERODROMES = Path(__file__).resolve().parent.parent / "db" / "aerodromes.json"

# A fix repeated within this radius is the same fix reported twice, not a slow aircraft.
_STILL_NM = 0.01
# Faster than this across the ground and the step is a discontinuity, not flight.
_MAX_KT = 900

# The denominators move slowly and cost a scan of every fix in the window; counting them once
# per window is enough, and stops a tab switch from costing twenty seconds.
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_S = 300


def _cached(key: str, produce):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    value = produce()
    _CACHE[key] = (now, value)
    return value


def _aerodromes() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_AERODROMES.read_text(encoding="utf-8"))["aerodromes"]
    except (OSError, KeyError, ValueError):
        return {}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


@router.get("/case/{event_id}")
def case_file(event_id: int) -> dict[str, Any]:
    """Everything known about one event, in one round trip.

    A detection is a claim. This returns the material needed to test it: which detector made
    the claim, the numbers it was made from, whether the tracks underneath it were sound, and
    what the same aircraft have done before.
    """
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, event_time, event_type, severity, lat, lon, place_icao, place_name,
                       altitude_ft, callsigns, icao24, registration, metrics, source,
                       provenance, external_ref, ingested_at, detector,
                       invalidated_at, invalidation_reason, raw
                  FROM safety_event
                 WHERE id = %s
                """,
                (event_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"no event {event_id}")

            columns = [
                "id", "event_time", "event_type", "severity", "lat", "lon", "place_icao",
                "place_name", "altitude_ft", "callsigns", "icao24", "registration", "metrics",
                "source", "provenance", "external_ref", "ingested_at", "detector",
                "invalidated_at", "invalidation_reason", "raw",
            ]
            event = dict(zip(columns, row, strict=True))
            event["event_time"] = _iso(event["event_time"])
            event["ingested_at"] = _iso(event["ingested_at"])
            event["invalidated_at"] = _iso(event["invalidated_at"])

            icaos = event.get("icao24") or []
            callsigns = event.get("callsigns") or []

            # What else this aircraft pair / these aircraft have done. Overlap on either key:
            # a pair can recur with one callsign changed between flights.
            cur.execute(
                """
                SELECT id, event_time, event_type, severity, place_icao, callsigns,
                       ABS(EXTRACT(EPOCH FROM event_time - %s::timestamptz)) AS delta_s
                  FROM safety_event
                 WHERE id <> %s
                   AND (icao24 && %s::text[] OR callsigns && %s::text[])
                 ORDER BY event_time DESC
                 LIMIT 10
                """,
                (event["event_time"], event_id, icaos, callsigns),
            )
            related = [
                {
                    "id": other_id,
                    "event_time": _iso(other_time),
                    "event_type": kind,
                    "severity": severity,
                    "place_icao": place,
                    "callsigns": pair,
                    "delta_s": _num(delta),
                }
                for other_id, other_time, kind, severity, place, pair, delta in cur.fetchall()
            ]

            # Were the tracks under this event worth anything? Same test the replay pane
            # applies, restricted to the aircraft involved and the minutes around it.
            quality = {"aircraft": 0, "steps": 0, "repeated_fixes": 0, "implausible_segments": 0}
            if icaos:
                cur.execute(
                    """
                    WITH obs AS (
                        SELECT icao24, observed_at, lat, lon,
                               LAG(observed_at) OVER w AS prev_at,
                               LAG(lat) OVER w AS prev_lat,
                               LAG(lon) OVER w AS prev_lon
                          FROM aircraft_track
                         WHERE icao24 = ANY(%s)
                           AND observed_at BETWEEN %s::timestamptz - interval '5 minutes'
                                               AND %s::timestamptz + interval '5 minutes'
                        WINDOW w AS (PARTITION BY icao24 ORDER BY observed_at)
                    ), steps AS (
                        SELECT icao24,
                               EXTRACT(EPOCH FROM observed_at - prev_at) AS gap_s,
                               60 * sqrt(power(lat - prev_lat, 2)
                                         + power(cos(radians((lat + prev_lat) / 2)) * (lon - prev_lon), 2)) AS nm
                          FROM obs
                         WHERE prev_at IS NOT NULL
                    )
                    SELECT COUNT(DISTINCT icao24) AS aircraft,
                           COUNT(*) AS steps,
                           COUNT(*) FILTER (WHERE nm < %s) AS repeated,
                           COUNT(*) FILTER (WHERE nm >= %s
                                              AND nm / GREATEST(gap_s, 1) * 3600 > %s) AS implausible
                      FROM steps
                    """,
                    (icaos, event["event_time"], event["event_time"], _STILL_NM, _STILL_NM, _MAX_KT),
                )
                aircraft, steps, repeated, implausible = cur.fetchone()
                quality = {
                    "aircraft": aircraft or 0,
                    "steps": steps or 0,
                    "repeated_fixes": repeated or 0,
                    "implausible_segments": implausible or 0,
                }

    return {"event": event, "related": related, "quality": quality}


@router.get("/rates")
def rates(days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    """Events per aircraft seen, not events per week.

    A raw count ranks aerodromes by how busy they are. The denominator here is the number of
    distinct aircraft the receivers saw within 10 NM of the aerodrome in the same window —
    a proxy for movements, and the field is named `aircraft` so nobody mistakes it for one.

    The whole payload is cached: counting distinct aircraft across every fix in the window is
    a full scan, and this box has one core. Freshness is reported rather than implied.
    """
    return _cached(f"rates:{days}", lambda: _rates(days))


def _rates(days: int) -> dict[str, Any]:
    aerodromes = _aerodromes()
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT place_icao,
                       COUNT(*) AS events,
                       COUNT(*) FILTER (WHERE severity = 'high') AS high
                  FROM safety_event
                 WHERE invalidated_at IS NULL
                   AND event_time > now() - make_interval(days => %s)
                 GROUP BY 1
                """,
                (days,),
            )
            place_events = {code: (count, high or 0) for code, count, high in cur.fetchall()}

            # One pass over the window, every aerodrome at once. Four separate scans with the
            # trigonometry inline took twenty seconds; the cosine is a constant per aerodrome
            # and the distance is only ever compared against a radius, so both are hoisted.
            def count_traffic() -> dict[str, int]:
                candidates = {code: aerodromes[code] for code in place_events if code in aerodromes}
                if not candidates:
                    return {}
                # Explicit casts: without them the VALUES list is all text and `a.lat - 0.2` has no operator.
                values_sql = ", ".join(["(%s::text, %s::double precision, %s::double precision, %s::double precision)"] * len(candidates))
                # Order follows the SQL text: the VALUES list comes first, the window last.
                params: list[Any] = []
                for code, info in candidates.items():
                    params.extend([code, info["lat"], info["lon"], math.cos(math.radians(info["lat"]))])
                params.append(days)
                cur.execute(
                    f"""
                    WITH aeros(code, lat, lon, scale) AS (VALUES {values_sql}),
                    hits AS (
                        SELECT a.code, t.icao24
                          FROM aircraft_track t
                          JOIN aeros a
                            ON t.lat BETWEEN a.lat - 0.2 AND a.lat + 0.2
                           AND t.lon BETWEEN a.lon - 0.2 AND a.lon + 0.2
                         WHERE t.observed_at > now() - make_interval(days => %s)
                           AND power(t.lat - a.lat, 2)
                               + power(a.scale * (t.lon - a.lon), 2) <= power(10.0 / 60.0, 2)
                    )
                    SELECT code, COUNT(DISTINCT icao24) FROM hits GROUP BY 1
                    """,
                    tuple(params),
                )
                return {code: count for code, count in cur.fetchall()}

            # Unplaced events have no code; joining the key list on None is a crash, not a cache miss.
            key_places = ",".join(sorted(code for code in place_events if code))
            place_traffic = _cached(f"traffic:{days}:{key_places}", count_traffic)

            places = []
            for code, (count, high) in place_events.items():
                aircraft = place_traffic.get(code)
                rate = round(count / aircraft * 100, 2) if aircraft else None
                places.append(
                    {
                        "place_icao": code,
                        "place_name": (aerodromes.get(code) or {}).get("name") if code else None,
                        "events": count,
                        "high": high,
                        "aircraft": aircraft,
                        "per_100_aircraft": rate,
                    }
                )
            places.sort(key=lambda item: (-(item["per_100_aircraft"] or -1), -item["events"]))

            # Hour of day, against the traffic that was actually there at that hour.
            cur.execute(
                """
                SELECT EXTRACT(HOUR FROM event_time)::int AS hour, COUNT(*)
                  FROM safety_event
                 WHERE invalidated_at IS NULL
                   AND event_time > now() - make_interval(days => %s)
                 GROUP BY 1
                """,
                (days,),
            )
            hour_events = {int(hour): count for hour, count in cur.fetchall()}

            cur.execute(
                """
                SELECT EXTRACT(HOUR FROM observed_at)::int AS hour, COUNT(DISTINCT icao24)
                  FROM aircraft_track
                 WHERE observed_at > now() - make_interval(days => %s)
                 GROUP BY 1
                """,
                (days,),
            )
            hour_traffic = {int(hour): count for hour, count in cur.fetchall()}

            hours = []
            for hour in sorted(set(hour_events) | set(hour_traffic)):
                count = hour_events.get(hour, 0)
                aircraft = hour_traffic.get(hour)
                hours.append(
                    {
                        "hour": hour,
                        "events": count,
                        "aircraft": aircraft,
                        "per_100_aircraft": round(count / aircraft * 100, 2) if aircraft else None,
                    }
                )

            cur.execute(
                """
                SELECT event_type, COUNT(*)
                  FROM safety_event
                 WHERE invalidated_at IS NULL
                   AND event_time > now() - make_interval(days => %s)
                 GROUP BY 1 ORDER BY 2 DESC
                """,
                (days,),
            )
            kinds = [{"event_type": kind, "events": count} for kind, count in cur.fetchall()]

            cur.execute(
                """
                SELECT MIN(event_time), MAX(event_time), COUNT(*)
                  FROM safety_event
                 WHERE invalidated_at IS NULL
                """
            )
            first, last, total = cur.fetchone()

    return {
        "days": days,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "window": {"first_event": _iso(first), "last_event": _iso(last), "events": total or 0},
        "places": places,
        "hours": hours,
        "types": kinds,
        "note": (
            "per_100_aircraft is events per 100 distinct aircraft seen within 10 NM of the "
            "aerodrome (or active in that hour) in the same window — a proxy for movements, "
            "not a certified count."
        ),
    }


@router.get("/recurrence")
def recurrence(days: int = Query(30, ge=1, le=365), min_events: int = Query(3, ge=2, le=20)) -> dict[str, Any]:
    """Aircraft and pairs that keep coming back.

    One event is an incident; the fourth occurrence of the same pair within a month is a
    pattern, and the difference matters when deciding whether to look at the aircraft or at
    the procedure.
    """
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT callsigns, COUNT(*) AS events,
                       MIN(event_time) AS first_seen, MAX(event_time) AS last_seen,
                       ARRAY_AGG(DISTINCT event_type) AS kinds,
                       ARRAY_AGG(DISTINCT place_icao) FILTER (WHERE place_icao IS NOT NULL) AS places
                  FROM safety_event
                 WHERE invalidated_at IS NULL
                   AND event_time > now() - make_interval(days => %s)
                   AND callsigns IS NOT NULL
                   AND array_length(callsigns, 1) > 0
                 GROUP BY callsigns
                HAVING COUNT(*) >= %s
                 ORDER BY events DESC
                 LIMIT 40
                """,
                (days, min_events),
            )
            pairs = [
                {
                    "callsigns": list(pair),
                    "events": count,
                    "first_seen": _iso(first),
                    "last_seen": _iso(last),
                    "types": sorted(kinds or []),
                    "places": sorted(places or []),
                }
                for pair, count, first, last, kinds, places in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT callsign, COUNT(*) AS events,
                       MIN(event_time) AS first_seen, MAX(event_time) AS last_seen
                  FROM (
                        SELECT UNNEST(callsigns) AS callsign, event_time
                          FROM safety_event
                         WHERE invalidated_at IS NULL
                           AND event_time > now() - make_interval(days => %s)
                           AND callsigns IS NOT NULL
                           AND array_length(callsigns, 1) > 0
                       ) AS expanded
                 GROUP BY 1
                HAVING COUNT(*) >= %s
                 ORDER BY events DESC
                 LIMIT 40
                """,
                (days, min_events),
            )
            aircraft = [
                {"callsign": callsign, "events": count, "first_seen": _iso(first), "last_seen": _iso(last)}
                for callsign, count, first, last in cur.fetchall()
            ]

    return {"days": days, "min_events": min_events, "pairs": pairs, "callsigns": aircraft}
