"""Turn what the detectors saw into events that live in the event store.

Until now the store held only what somebody wrote down: a report, an upload. The
detectors computed conflicts and manoeuvres, showed them for fifteen seconds, and
forgot them. This module promotes the ones worth keeping into `safety_event` with
`source = 'derived'` — same shape as a report, distinguishable by `source`, and
carrying the arithmetic that produced it in `metrics`.

Three deliberate restrictions:

* **Observed, not predicted.** A projection is a prompt to look, not a record. Only
  snapshot conflicts and manoeuvres that actually appear in the tracks are stored.
* **One row per aircraft per half hour.** Aircraft are re-detected on every poll; a
  deterministic `external_ref` lets the unique index absorb the repetition instead of
  turning one hold into forty rows.
* **A go-around is not an incident.** It is stored with `low` severity and a
  provenance sentence that says so, because the number of go-arounds at an airport is
  a normal-operations statistic, not an accident list.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

BUCKET_SECONDS = 1800  # one event per aircraft (or pair) per half hour

# What the store is allowed to keep, and how bad it is.
SEVERITY = {
    "loss_of_separation": "high",
    "unstable_approach": "medium",
    "go_around": "low",
    "holding_pattern": "low",
}

KEPT_KINDS = {"go_around", "unstable_approach"}

# Approximate aerodrome lookup radius: beyond this the event is not "at" an airport.
AERODROME_RADIUS_NM = 15.0


_aerodrome_cache: list[dict[str, Any]] | None = None


def load_aerodromes(path: Any = None) -> list[dict[str, Any]]:
    """The committed aerodrome reference, as a list the matcher can walk."""
    global _aerodrome_cache
    if _aerodrome_cache is not None:
        return _aerodrome_cache
    import json
    from pathlib import Path

    target = path or Path(__file__).resolve().parents[1] / "db" / "aerodromes.json"
    try:
        payload = json.loads(Path(target).read_text(encoding="utf-8"))
        _aerodrome_cache = [
            {"icao": code.upper(), "name": row.get("name"), "lat": row.get("lat"), "lon": row.get("lon")}
            for code, row in (payload.get("aerodromes") or {}).items()
        ]
    except Exception:  # noqa: BLE001 - naming the airport is a nicety, not a requirement
        _aerodrome_cache = []
    return _aerodrome_cache


def _bucket(epoch: float) -> int:
    return int(epoch // BUCKET_SECONDS)


def _nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    dy = (lat2 - lat1) * 60.0
    dx = (lon2 - lon1) * 60.0 * max(0.05, abs(math.cos(math.radians((lat1 + lat2) / 2.0))))
    return math.hypot(dx, dy)


def _nearest_aerodrome(lat: float, lon: float, aerodromes: Iterable[dict[str, Any]] | None):
    """Name the airport only when the event is actually near one."""
    best = None
    for field in aerodromes or ():
        try:
            place_lat = float(field["lat"])
            place_lon = float(field["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = _nm(lat, lon, place_lat, place_lon)
        if distance <= AERODROME_RADIUS_NM and (best is None or distance < best[0]):
            best = (distance, field)
    if best is None:
        return None, None
    field = best[1]
    return field.get("icao") or field.get("iata"), field.get("name")


def _observed(epoch: float | None) -> datetime:
    return datetime.fromtimestamp(float(epoch or 0), tz=timezone.utc)


def from_conflicts(
    conflicts: Iterable[dict[str, Any]],
    rules: Any,
    observed_at: float | None,
    aerodromes: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One event per pair currently inside the separation minimum."""
    events: list[dict[str, Any]] = []
    moment = _observed(observed_at)
    for item in conflicts:
        lat, lon = item.get("lat"), item.get("lon")
        if lat is None or lon is None:
            continue
        icao, place = _nearest_aerodrome(lat, lon, aerodromes)
        pair = sorted([str(item.get("a", "")), str(item.get("b", ""))])
        separation = item.get("separation_nm")
        vertical = item.get("vertical_ft")
        events.append(
            {
                "event_time": moment,
                "event_type": "loss_of_separation",
                "severity": SEVERITY["loss_of_separation"],
                "lat": lat,
                "lon": lon,
                "place_icao": icao,
                "place_name": place,
                "altitude_ft": item.get("altitude_a_ft") or item.get("altitude_ft"),
                "callsigns": [c for c in pair if c],
                "icao24": [i for i in (item.get("icao24_a"), item.get("icao24_b")) if i],
                "metrics": {
                    "separation_nm": separation,
                    "vertical_ft": vertical,
                    "min_separation_nm": getattr(rules, "min_separation_nm", None),
                    "min_vertical_ft": getattr(rules, "min_vertical_ft", None),
                    "detector": "snapshot_separation",
                },
                "source": "derived",
                # Deterministic: the unique index turns a re-observation into a no-op.
                "external_ref": f"derived:los:{'-'.join(pair)}:{_bucket(moment.timestamp())}",
                "provenance": (
                    f"Derived from an OpenSky position snapshot at {moment.isoformat()}: "
                    f"{pair[0]} and {pair[1]} were {separation} NM apart horizontally"
                    + (f" and {vertical} ft vertically" if vertical is not None else " with vertical separation unknown")
                    + f", against minima of {getattr(rules, 'min_separation_nm', '?')} NM / "
                    f"{getattr(rules, 'min_vertical_ft', '?')} ft. Geometries are computed by the server; "
                    "no language model contributes to the numbers."
                ),
                "raw": {"a": item.get("a"), "b": item.get("b"), "separation_nm": separation, "vertical_ft": vertical},
            }
        )
    return events


def from_motion(
    motion_events: Iterable[dict[str, Any]],
    observed_at: float | None,
    aerodromes: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One event per manoeuvre worth keeping: go-arounds and unstable approaches."""
    events: list[dict[str, Any]] = []
    moment = _observed(observed_at)
    for item in motion_events:
        kind = item.get("kind")
        if kind not in KEPT_KINDS:
            continue
        lat, lon = item.get("lat"), item.get("lon")
        if lat is None or lon is None:
            continue
        icao, place = _nearest_aerodrome(lat, lon, aerodromes)
        callsign = (item.get("callsign") or "").strip()
        reference = item.get("icao24") or callsign or "unknown"
        events.append(
            {
                "event_time": moment,
                "event_type": kind,
                "severity": SEVERITY.get(kind, "unknown"),
                "lat": lat,
                "lon": lon,
                "place_icao": icao,
                "place_name": place,
                "altitude_ft": item.get("altitude_ft"),
                "callsigns": [callsign] if callsign else [],
                "icao24": [item.get("icao24")] if item.get("icao24") else [],
                "metrics": {"detail": item.get("detail"), "detector": "track_motion"},
                "source": "derived",
                "external_ref": f"derived:{kind}:{reference}:{_bucket(moment.timestamp())}",
                "provenance": (
                    f"Derived from ADS-B tracks by the track-motion detector at {moment.isoformat()}: "
                    f"{callsign or reference} {item.get('detail')}. "
                    + (
                        "A go-around is a normal, safe manoeuvre and is recorded as normal operations, "
                        "not as an incident. "
                        if kind == "go_around"
                        else "An unstable approach is a heuristic on height, speed and descent rate; "
                        "it ranks what to review and is not a finding. "
                    )
                    + "No language model contributes to the arithmetic."
                ),
                "raw": dict(item),
            }
        )
    return events


def from_snapshot(
    payload: dict[str, Any],
    rules: Any,
    aerodromes: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Everything worth storing from one live snapshot."""
    observed = payload.get("observed_at")
    detections = payload.get("detections") or {}
    motion = payload.get("motion") or {}
    return from_conflicts(detections.get("conflicts") or [], rules, observed, aerodromes) + from_motion(
        motion.get("events") or [], observed, aerodromes
    )
