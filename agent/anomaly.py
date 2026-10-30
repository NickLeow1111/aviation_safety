"""What looks unusual about the traffic in front of you.

An analyst asking "any unusual flight path?" is asking two different questions at
once: *is anything wrong with this aircraft right now* (emergency squawk, absurd
climb rate, fast and low) and *is anything changing shape* (a turn far sharper than
the aircraft's track has been). The first is answerable from one snapshot. The second
needs the previous snapshot, so this module keeps a small bounded history keyed by
ICAO address.

Everything here is a heuristic on ADS-B, and the output says so. Nothing in this
file claims an aircraft is in trouble — it reports what the data looks like and
leaves the judgement to the person reading it.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

# Thresholds are deliberately conservative: a flag should be worth looking at.
LOW_ALTITUDE_FT = 3000
LOW_ALTITUDE_SPEED_KT = 250
EXTREME_VERTICAL_FPM = 6000
GROUND_SPEED_ON_GROUND_KT = 60
LOITER_SPEED_KT = 80
LOITER_MIN_ALTITUDE_FT = 1500
# A turn sharper than this between two snapshots is worth a look. A 90-degree turn
# is ordinary in a hold; 30 degrees in ten seconds is not.
TURN_DEGREES = 30.0
TURN_MIN_SECONDS = 5.0
HISTORY_TTL_SECONDS = 300.0

EMERGENCY_SQUAWKS = {"7500": "hijack", "7600": "radio failure", "7700": "general emergency"}

# icao24 -> (track_deg, monotonic timestamp)
_previous: dict[str, tuple[float, float]] = {}


def _label(aircraft: dict[str, Any]) -> str:
    return (aircraft.get("callsign") or aircraft.get("icao24") or "unknown").strip()


def reset_history() -> None:
    """Clear the turn detector. Used by tests so state cannot leak between them."""
    _previous.clear()


def _turn_rate_flag(aircraft: dict[str, Any], now: float) -> dict[str, Any] | None:
    icao24 = str(aircraft.get("icao24") or "")
    track = aircraft.get("track_deg")
    if not icao24 or track is None:
        return None

    previous = _previous.get(icao24)
    _previous[icao24] = (float(track), now)
    if previous is None:
        return None

    last_track, last_time = previous
    elapsed = now - last_time
    if elapsed < TURN_MIN_SECONDS:
        return None

    # Shortest angular difference, so 350 -> 10 reads as a 20-degree turn.
    delta = abs((float(track) - last_track + 180.0) % 360.0 - 180.0)
    if delta < TURN_DEGREES:
        return None

    return {
        "kind": "sharp_turn",
        "callsign": _label(aircraft),
        "icao24": icao24,
        "detail": f"turned {delta:.0f}° in {elapsed:.0f}s",
        "track_deg": float(track),
        "lat": aircraft.get("lat"),
        "lon": aircraft.get("lon"),
        "altitude_ft": aircraft.get("altitude_ft"),
    }


def find(
    aircraft: Iterable[dict[str, Any]],
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Return the unusual-looking aircraft in one snapshot, with reasons."""
    moment = now()
    flags: list[dict[str, Any]] = []

    for item in aircraft:
        if item.get("lat") is None or item.get("lon") is None:
            continue

        callsign = _label(item)
        icao24 = str(item.get("icao24") or "")
        altitude = item.get("altitude_ft")
        speed = item.get("ground_speed_kt")
        vertical = item.get("vertical_rate_fpm")
        squawk = str(item.get("squawk") or "").strip()
        on_ground = bool(item.get("on_ground"))

        def flag(kind: str, detail: str, **extra: Any) -> dict[str, Any]:
            entry = {
                "kind": kind,
                "callsign": callsign,
                "icao24": icao24,
                "detail": detail,
                "lat": item.get("lat"),
                "lon": item.get("lon"),
                "altitude_ft": altitude,
                "ground_speed_kt": speed,
            }
            entry.update(extra)
            flags.append(entry)
            return entry

        if squawk in EMERGENCY_SQUAWKS:
            flag("emergency_squawk", f"squawking {squawk} ({EMERGENCY_SQUAWKS[squawk]})")

        if (
            not on_ground
            and altitude is not None
            and altitude < LOW_ALTITUDE_FT
            and speed is not None
            and speed > LOW_ALTITUDE_SPEED_KT
        ):
            flag("low_and_fast", f"{speed:.0f} kt at {altitude:.0f} ft")

        if vertical is not None and abs(vertical) > EXTREME_VERTICAL_FPM:
            direction = "climbing" if vertical > 0 else "descending"
            flag("extreme_vertical_rate", f"{direction} at {abs(vertical):.0f} ft/min")

        if on_ground and speed is not None and speed > GROUND_SPEED_ON_GROUND_KT:
            flag("fast_on_ground", f"{speed:.0f} kt on the ground")

        if (
            not on_ground
            and speed is not None
            and speed < LOITER_SPEED_KT
            and altitude is not None
            and altitude > LOITER_MIN_ALTITUDE_FT
        ):
            flag("slow_at_altitude", f"{speed:.0f} kt at {altitude:.0f} ft")

        turn = _turn_rate_flag(item, moment)
        if turn:
            flags.append(turn)

    _prune(moment)

    if not flags:
        summary = "No unusual flight behaviour detected in this snapshot."
    else:
        kinds = sorted({flag["kind"] for flag in flags})
        summary = (
            f"{len(flags)} unusual signal(s) across {len({flag['icao24'] for flag in flags})} aircraft: "
            + ", ".join(kinds)
        )

    return {
        "flags": flags,
        "summary": summary,
        "thresholds": {
            "low_altitude_ft": LOW_ALTITUDE_FT,
            "low_altitude_speed_kt": LOW_ALTITUDE_SPEED_KT,
            "extreme_vertical_fpm": EXTREME_VERTICAL_FPM,
            "turn_degrees": TURN_DEGREES,
        },
        "disclaimer": (
            "Heuristic signs on a position snapshot. They rank what to look at; they "
            "do not mean an aircraft is in difficulty."
        ),
    }


def _prune(now: float) -> None:
    stale = [key for key, (_, seen) in _previous.items() if now - seen > HISTORY_TTL_SECONDS]
    for key in stale:
        _previous.pop(key, None)
