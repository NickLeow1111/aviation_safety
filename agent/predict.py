"""Where the traffic is *going*, not just where it is.

A snapshot can only report a loss of separation after it has happened. Dead reckoning
each aircraft along its reported track of flight and ground speed, then solving for the
closest point of approach, turns that into a warning with a time on it: *these two will
be 1.8 NM apart in 46 seconds*.

The assumption is stated up front, because it matters: constant track, constant speed,
constant vertical rate. An aircraft that turns will not do this. What the projection
buys is the ninety seconds between "about to happen" and "happening", which is the
whole difference between a monitoring tool and a log.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

DEFAULT_HORIZON_SECONDS = 120.0
STEP_SECONDS = 5.0
# Only pairs this close are worth projecting: beyond it, nothing in two minutes.
PAIR_PREFILTER_NM = 40.0


def _offset_nm(lat: float, from_lat: float) -> float:
    """Longitude degrees shrink with latitude; convert them to the same units."""
    return 60.0 * max(0.05, abs(math.cos(math.radians(from_lat))))


def separation_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth separation. Accurate to a fraction of a percent over a local view."""
    dy = (lat2 - lat1) * 60.0
    dx = (lon2 - lon1) * _offset_nm(lat1, (lat1 + lat2) / 2.0)
    return math.hypot(dx, dy)


def project(lat: float, lon: float, track_deg: float | None, speed_kt: float | None, seconds: float):
    """Straight-line position `seconds` from now at the reported track and speed."""
    if track_deg is None or speed_kt is None or speed_kt <= 0:
        return lat, lon
    distance = speed_kt * seconds / 3600.0  # kt is nm per hour
    radians = math.radians(track_deg)
    north = distance * math.cos(radians)
    east = distance * math.sin(radians)
    return lat + north / 60.0, lon + east / _offset_nm(lat, lat)


def _altitude_at(item: dict[str, Any], seconds: float) -> float | None:
    altitude = item.get("altitude_ft")
    if altitude is None:
        return None
    vertical = item.get("vertical_rate_fpm") or 0.0
    return altitude + vertical * seconds / 60.0


def closest_approach(a: dict[str, Any], b: dict[str, Any], horizon_s: float = DEFAULT_HORIZON_SECONDS):
    """The tightest predicted separation inside the horizon, with when it happens."""
    best = None
    steps = int(horizon_s / STEP_SECONDS) + 1
    for index in range(steps):
        seconds = index * STEP_SECONDS
        alat, alon = project(a["lat"], a["lon"], a.get("track_deg"), a.get("ground_speed_kt"), seconds)
        blat, blon = project(b["lat"], b["lon"], b.get("track_deg"), b.get("ground_speed_kt"), seconds)
        distance = separation_nm(alat, alon, blat, blon)
        if best is None or distance < best["separation_nm"]:
            alt_a = _altitude_at(a, seconds)
            alt_b = _altitude_at(b, seconds)
            vertical = None if alt_a is None or alt_b is None else abs(alt_a - alt_b)
            best = {
                "separation_nm": round(distance, 2),
                "time_s": int(seconds),
                "vertical_ft": None if vertical is None else int(vertical),
                "altitude_a_ft": None if alt_a is None else int(alt_a),
                "altitude_b_ft": None if alt_b is None else int(alt_b),
            }
    return best


def _label(item: dict[str, Any]) -> str:
    return (item.get("callsign") or item.get("icao24") or "unknown").strip()


def evaluate(
    aircraft: Iterable[dict[str, Any]],
    rules: Any,
    horizon_s: float = DEFAULT_HORIZON_SECONDS,
) -> dict[str, Any]:
    """Pairs predicted to be inside the separation minima within the horizon."""
    candidates = [
        item
        for item in aircraft
        if item.get("lat") is not None
        and item.get("lon") is not None
        and item.get("ground_speed_kt") is not None
        and not item.get("on_ground")
    ]

    predictions: list[dict[str, Any]] = []
    for first in range(len(candidates)):
        for second in range(first + 1, len(candidates)):
            a, b = candidates[first], candidates[second]
            current = separation_nm(a["lat"], a["lon"], b["lat"], b["lon"])
            if current > PAIR_PREFILTER_NM:
                continue

            closest = closest_approach(a, b, horizon_s)
            if closest is None:
                continue
            if closest["separation_nm"] >= rules.min_separation_nm:
                continue
            if closest["vertical_ft"] is not None and closest["vertical_ft"] >= rules.min_vertical_ft:
                continue
            # Only convergence is interesting here: a pair already inside the minimum
            # is reported by the snapshot check, and listing it twice is noise.
            if closest["separation_nm"] >= current - 0.1:
                continue

            predictions.append(
                {
                    "type": "predicted_conflict",
                    "a": _label(a),
                    "b": _label(b),
                    "current_nm": round(current, 2),
                    "predicted_nm": closest["separation_nm"],
                    "time_to_cpa_s": closest["time_s"],
                    "vertical_ft": closest["vertical_ft"],
                    "altitude_a_ft": closest["altitude_a_ft"],
                    "altitude_b_ft": closest["altitude_b_ft"],
                    "lat": (a["lat"] + b["lat"]) / 2.0,
                    "lon": (a["lon"] + b["lon"]) / 2.0,
                }
            )

    predictions.sort(key=lambda item: (item["time_to_cpa_s"], item["predicted_nm"]))

    return {
        "horizon_s": int(horizon_s),
        "conflicts": predictions,
        "summary": (
            f"No pair is predicted to lose separation within {int(horizon_s)} seconds."
            if not predictions
            else f"{len(predictions)} pair(s) predicted inside the minimum within {int(horizon_s)}s: "
            + "; ".join(
                f"{p['a']}/{p['b']} {p['predicted_nm']} NM in {p['time_to_cpa_s']}s"
                for p in predictions[:4]
            )
        ),
        "disclaimer": (
            "Dead reckoning from one snapshot: constant track, speed and vertical rate. "
            "It is a projection, not a radar conflict alert, and a turn invalidates it."
        ),
    }
