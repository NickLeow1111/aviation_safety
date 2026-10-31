"""Flight phase: on the ground, in transition, or established airborne.

A single altitude floor was the first fix, and it was the right shape but the wrong
instrument. It kept a rolling-out aircraft out of the separation picture — correct — while
also being ready to drop a genuine encounter at 80 ft on climb-out, which would be a
safety finding discarded as noise.

So the question is not "how high" but "what is this aircraft doing". Three answers:

    ground      — on a surface: taxiing, holding, rolling out, parked.
    transition  — low and changing height quickly: rotating, climbing out, on approach,
                  about to touch down. Genuinely flying, and genuinely worth comparing
                  against other traffic.
    airborne    — everything established above the terminal area.

Ground traffic is what separation minima are not about. Transition traffic is the case a
flat floor would have quietly lost, so it stays in the comparison and is labelled.
"""
from __future__ import annotations

from typing import Any

# Bumped whenever the rules below change. Every stored judgement records the version that
# made it, so a re-judgement after a rule change is attributable instead of anonymous —
# and `reclassify --auto` uses this to notice that the rules moved.
CLASSIFIER_VERSION = "phase-3"

GROUND = "ground"
TRANSITION = "transition"
AIRBORNE = "airborne"

# At or below the surface band: Singapore's aerodromes sit between 22 ft (Changi) and
# 65 ft (Paya Lebar), and a position reported here is wheels-on or wheels-adjacent.
SURFACE_FT = 50.0

# Slow and low is taxiing.
TAXI_CEILING_FT = 100.0
TAXI_SPEED_KT = 40.0

# Nothing flies level at a hundred feet and a hundred and fifty knots over Changi. Inside
# this band, level flight is a rollout: the aircraft has landed and is decelerating down
# the runway, still fast enough to look like traffic to a speed test. A landing aircraft
# at the same height is still descending hard (the flare flattens late), and a departure
# is climbing hard, so both stay in transition.
TOUCHDOWN_BAND_FT = 150.0
LEVEL_RATE_FPM = 250.0

# Low and climbing or descending hard is a takeoff or a landing, not a cruise.
TRANSITION_CEILING_FT = 2000.0
TRANSITION_RATE_FPM = 500.0

# Low and level: in the circuit, or holding a low-level clearance.
LOW_CEILING_FT = 1000.0

PHASES = (GROUND, TRANSITION, AIRBORNE)

# --------------------------------------------------------------------------- #
# Order of precedence
# --------------------------------------------------------------------------- #
# The rules are checked in this order and the first match wins. The order is the
# specification; the thresholds are inputs to it.
#
#   1. `on_ground` is true                      -> GROUND      (the transponder's own word)
#   2. altitude is unknown                      -> AIRBORNE    (unknown stays in play)
#   3. altitude <= SURFACE_FT (50 ft)           -> GROUND      (surface band)
#   4. altitude <= TOUCHDOWN_BAND_FT (150 ft)
#      and |vertical rate| < LEVEL_RATE_FPM     -> GROUND      (rollout: fast, low, level)
#   5. altitude <= TAXI_CEILING_FT (100 ft)
#      and |vertical rate| < LEVEL_RATE_FPM
#      and speed <= TAXI_SPEED_KT               -> GROUND      (taxiing)
#   6. altitude <= TRANSITION_CEILING_FT (2000 ft)
#      and |vertical rate| >= TRANSITION_RATE_FPM -> TRANSITION (climbing out / on approach)
#   7. altitude <= LOW_CEILING_FT (1000 ft)     -> TRANSITION  (low and level: circuit)
#   8. otherwise                                -> AIRBORNE
#
# Rule 4 is the one that overlaps with the low-and-level case, and it is deliberately
# placed before rule 5: inside 150 ft, level flight is a rollout whatever the speed. That
# is what keeps a decelerating aircraft on the runway out of the picture without touching
# a landing aircraft (descending hard, rule 6) or a departing one (climbing hard, rule 6).
#
# A missing vertical rate counts as level (rules 4 and 5); a missing speed counts as slow
# (rule 5). Both biases point towards GROUND, which is the conservative direction for a
# separation check: a false GROUND loses one comparison, a false AIRBORNE fills the map
# with arrivals that never were conflicts.


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def classify(aircraft: dict[str, Any]) -> str:
    """Which phase this position belongs to.

    Unknown height is treated as airborne: an aircraft we cannot place vertically is
    still traffic, and the vertical test in `detect.evaluate` will treat it as unknown
    rather than safe. Anything else would silently drop coverage.
    """
    if aircraft.get("on_ground"):
        return GROUND

    altitude = _number(aircraft.get("altitude_ft"))
    if altitude is None:
        return AIRBORNE

    if altitude <= SURFACE_FT:
        return GROUND

    vertical = _number(aircraft.get("vertical_rate_fpm"))
    speed = _number(aircraft.get("ground_speed_kt"))

    level = vertical is None or abs(vertical) < LEVEL_RATE_FPM

    if altitude <= TOUCHDOWN_BAND_FT and level:
        return GROUND

    if altitude <= TAXI_CEILING_FT and level:
        slow = speed is None or speed <= TAXI_SPEED_KT
        if slow:
            return GROUND

    if altitude <= TRANSITION_CEILING_FT:
        if vertical is not None and abs(vertical) >= TRANSITION_RATE_FPM:
            return TRANSITION
        if altitude <= LOW_CEILING_FT:
            return TRANSITION

    return AIRBORNE


def is_airborne(aircraft: dict[str, Any]) -> bool:
    """Whether this position is traffic rather than something on a surface."""
    return classify(aircraft) != GROUND


def explain(aircraft: dict[str, Any]) -> tuple[str, str]:
    """The phase and the reason for it — for tests, audits and the board.

    Recomputes through `classify` so the two can never disagree; the reason text names the
    rule that fired, by its number in the table above.
    """
    if aircraft.get("on_ground"):
        return GROUND, "rule 1: on_ground flag"
    altitude = _number(aircraft.get("altitude_ft"))
    if altitude is None:
        return AIRBORNE, "rule 2: height unknown, kept in play"
    if altitude <= SURFACE_FT:
        return GROUND, f"rule 3: at or below the {SURFACE_FT:.0f} ft surface band"

    vertical = _number(aircraft.get("vertical_rate_fpm"))
    speed = _number(aircraft.get("ground_speed_kt"))
    level = vertical is None or abs(vertical) < LEVEL_RATE_FPM

    if altitude <= TOUCHDOWN_BAND_FT and level:
        return GROUND, f"rule 4: level inside {TOUCHDOWN_BAND_FT:.0f} ft (rollout)"
    if altitude <= TAXI_CEILING_FT and level and (speed is None or speed <= TAXI_SPEED_KT):
        return GROUND, f"rule 5: slow and level below {TAXI_CEILING_FT:.0f} ft (taxiing)"
    if altitude <= TRANSITION_CEILING_FT and vertical is not None and abs(vertical) >= TRANSITION_RATE_FPM:
        return TRANSITION, f"rule 6: {abs(vertical):.0f} fpm at {altitude:.0f} ft"
    if altitude <= LOW_CEILING_FT:
        return TRANSITION, f"rule 7: low ({altitude:.0f} ft) but not established"
    return AIRBORNE, "rule 8: established traffic"


def describe(phase: str) -> str:
    """One line for a human reading an event."""
    return {
        GROUND: "on the ground",
        TRANSITION: "in transition (low, height changing, or in the circuit)",
        AIRBORNE: "established airborne",
    }.get(phase, phase)
