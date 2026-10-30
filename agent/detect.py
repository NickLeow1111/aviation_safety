"""Separation and speed checks over live ADS-B.

This answers two questions a controller asks of a traffic picture — *who is too
close* and *who is too fast* — with arithmetic the user can see, not with a model
guessing at a table of numbers.

What this is not: a conflict predictor. ADS-B positions arrive every 5–15 s, MLAT
gaps are real, and there is no intent, no route and no vertical-rate trend in the
snapshot. So a pair inside the threshold is reported as *currently inside it*, not
as "will lose separation". The wording in the output says exactly that.

Thresholds are the user's, stored as JSON so the assistant, the API and the map all
apply the same numbers.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Sequence

EARTH_RADIUS_NM = 3440.065

DEFAULTS = {
    "min_separation_nm": 3.0,
    "min_vertical_ft": 1000,
    "max_speed_kt": 480,
    "ignore_ground": True,
    "enabled": True,
}


@dataclass(frozen=True)
class Rules:
    min_separation_nm: float = 3.0
    min_vertical_ft: int = 1000
    max_speed_kt: int = 480
    ignore_ground: bool = True
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: Any) -> "Rules":
        """Accept anything stored in the database, however stale or malformed."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        if not isinstance(value, dict):
            value = {}

        def number(key: str, cast: type) -> Any:
            raw = value.get(key, DEFAULTS[key])
            try:
                return cast(float(raw))
            except (TypeError, ValueError):
                return DEFAULTS[key]

        return cls(
            min_separation_nm=max(0.1, number("min_separation_nm", float)),
            min_vertical_ft=max(0, number("min_vertical_ft", int)),
            max_speed_kt=max(1, number("max_speed_kt", int)),
            ignore_ground=bool(value.get("ignore_ground", DEFAULTS["ignore_ground"])),
            enabled=bool(value.get("enabled", DEFAULTS["enabled"])),
        )


def separation_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def _label(aircraft: dict[str, Any]) -> str:
    return (aircraft.get("callsign") or aircraft.get("icao24") or "unknown").strip()


def _usable(aircraft: dict[str, Any]) -> bool:
    return aircraft.get("lat") is not None and aircraft.get("lon") is not None


def evaluate(aircraft: Iterable[dict[str, Any]], rules: Rules) -> dict[str, Any]:
    """Apply the rules to one snapshot of traffic.

    Returns conflicts (inside horizontal *and* vertical minima, as a controller
    means it), overspeed, and the summary the UI and the assistant quote.
    """
    if not rules.enabled:
        return {
            "enabled": False,
            "conflicts": [],
            "overspeed": [],
            "summary": "Separation and speed checks are switched off.",
        }

    all_usable = [item for item in aircraft if _usable(item)]
    tracked = all_usable
    if rules.ignore_ground:
        tracked = [item for item in tracked if not item.get("on_ground")]

    conflicts: list[dict[str, Any]] = []
    for index, first in enumerate(tracked):
        for second in tracked[index + 1 :]:
            distance = separation_nm(first["lat"], first["lon"], second["lat"], second["lon"])
            if distance > rules.min_separation_nm:
                continue

            first_alt = first.get("altitude_ft")
            second_alt = second.get("altitude_ft")
            vertical = None
            if first_alt is not None and second_alt is not None:
                vertical = abs(int(first_alt) - int(second_alt))
                if vertical > rules.min_vertical_ft:
                    continue  # horizontally close but vertically separated: normal

            conflicts.append(
                {
                    "type": "separation",
                    "a": _label(first),
                    "b": _label(second),
                    "icao24_a": first.get("icao24"),
                    "icao24_b": second.get("icao24"),
                    "separation_nm": round(distance, 2),
                    "vertical_ft": vertical,
                    "lat": round((first["lat"] + second["lat"]) / 2, 5),
                    "lon": round((first["lon"] + second["lon"]) / 2, 5),
                }
            )
    conflicts.sort(key=lambda item: item["separation_nm"])

    overspeed: list[dict[str, Any]] = []
    for item in tracked:
        speed = item.get("ground_speed_kt")
        if speed is None or speed <= rules.max_speed_kt:
            continue
        overspeed.append(
            {
                "type": "overspeed",
                "callsign": _label(item),
                "icao24": item.get("icao24"),
                "ground_speed_kt": int(speed),
                "altitude_ft": item.get("altitude_ft"),
                "lat": item.get("lat"),
                "lon": item.get("lon"),
            }
        )
    overspeed.sort(key=lambda item: item["ground_speed_kt"], reverse=True)

    filtered = len(all_usable) - len(tracked)
    summary = (
        f"{len(tracked)} aircraft checked"
        + (f" ({filtered} on-ground excluded)" if filtered else "")
        + " against "
        f"{rules.min_separation_nm:g} NM / {rules.min_vertical_ft} ft separation and "
        f"{rules.max_speed_kt} kt max speed: "
        f"{len(conflicts)} inside separation, {len(overspeed)} above the speed limit."
    )
    return {
        "enabled": True,
        "rules": rules.as_dict(),
        "checked": len(tracked),
        "conflicts": conflicts,
        "overspeed": overspeed,
        "summary": summary,
        "disclaimer": (
            "A snapshot of position reports, not a conflict prediction: ADS-B updates "
            "every 5–15 s and the data carries no intent, route or vertical-rate trend."
        ),
    }


# --------------------------------------------------------------------------- #
# natural language -> rules
# --------------------------------------------------------------------------- #

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "half": 0.5,
}

_SEPARATION_RE = re.compile(
    r"(?:closer than|within|separation(?: minimum| standard| of| to| at)?|separated by|under|"
    r"call (?:it|that|anything)?\s*(?:at|within)|flag (?:it )?(?:at|within))\s+"
    r"(?P<value>\d+(?:\.\d+)?|[a-z]+)\s*(?P<unit>nm|nmi|nautical miles?|miles?|mi)\b",
    re.IGNORECASE,
)
_SEPARATION_ONLY_RE = re.compile(
    r"(?:separation|horizontal)[^0-9]{0,20}(?P<value>\d+(?:\.\d+)?|[a-z]+)\s*"
    r"(?P<unit>nm|nmi|nautical miles?|miles?|mi)\b",
    re.IGNORECASE,
)
# "limit to 450 knots", "limit of 450", "faster than 480 kt" — the preposition is
# the part people change and the part that silently broke the first version.
_SPEED_RE = re.compile(
    r"(?:faster than|above|over|exceeding|speed limit(?: of| to| at)?|"
    r"max(?:imum)? speed(?: of| to| at)?|limit(?: of| to)?)\s+"
    r"(?P<value>\d{2,4}|[a-z]+)\s*(?P<unit>kt|kts|knots?|km/h)\b",
    re.IGNORECASE,
)
_VERTICAL_RE = re.compile(
    r"(?:vertical(?:ly)? (?:separation|minimum)|within)\s*(?:of|to|at)?\s*"
    r"(?P<value>\d{2,5})\s*(?:ft|feet)\b",
    re.IGNORECASE,
)


def _as_number(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(_NUMBER_WORDS[raw.lower()]) if raw.lower() in _NUMBER_WORDS else None


def parse_rules(text: str, current: Rules | None = None) -> tuple[Rules | None, list[str]]:
    """Read threshold changes out of a sentence.

    Deliberately conservative: an instruction that does not clearly contain a
    threshold changes nothing and says so, rather than silently re-tuning the
    checks on a sentence that merely mentioned speed.
    """
    base = current or Rules()
    changes: list[str] = []
    updated = base

    separation = _SEPARATION_RE.search(text) or _SEPARATION_ONLY_RE.search(text)
    if separation:
        value = _as_number(separation.group("value"))
        if value is not None:
            unit = separation.group("unit").lower()
            nautical = value if unit.startswith(("nm", "nmi", "nautical")) else value * 0.868976
            updated = replace(updated, min_separation_nm=round(nautical, 2))
            changes.append(f"separation {round(nautical, 2):g} NM")

    speed = _SPEED_RE.search(text)
    if speed:
        value = _as_number(speed.group("value"))
        if value is not None:
            knots = value if not speed.group("unit").lower().startswith("km") else value * 0.539957
            updated = replace(updated, max_speed_kt=int(round(knots)))
            changes.append(f"speed limit {int(round(knots))} kt")

    vertical = _VERTICAL_RE.search(text)
    if vertical:
        value = _as_number(vertical.group("value"))
        if value is not None and 0 < value <= 20000:
            updated = replace(updated, min_vertical_ft=int(value))
            changes.append(f"vertical minimum {int(value)} ft")

    if re.search(r"\b(enable|turn on|switch on)\b.*\b(checks?|detection|alerts?)\b", text, re.IGNORECASE):
        updated = replace(updated, enabled=True)
        changes.append("detection on")
    if re.search(r"\b(disable|turn off|switch off)\b.*\b(checks?|detection|alerts?)\b", text, re.IGNORECASE):
        updated = replace(updated, enabled=False)
        changes.append("detection off")

    return (updated if changes else None), changes
