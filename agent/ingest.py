"""Turn an uploaded file into map events — and refuse to guess.

Everything here is pure: no database, no HTTP, no network. That is deliberate.
The interesting failures in an upload are all about *interpretation* (is `1°21'N`
a coordinate? is `WSSS` a place? is `05/03` a date? is a blank latitude a zero
latitude?), and those are far easier to test than to debug through a browser.

Rules this module holds to:

1. **Never invent a position.** A row that cannot be placed is returned with the
   reason and left out of the import, not dropped silently and not defaulted to
   (0, 0).
2. **Say how each position was obtained.** `position_source` is one of
   `coordinates`, `icao`, `alias`, `near`, so a reviewer can tell a typed
   latitude from a resolved airport code.
3. **Never invent a time.** A missing time is reported as such (`time_source:
   date_only`) rather than quietly becoming midnight, and the caller decides.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
AERODROMES_JSON = REPO_ROOT / "db" / "aerodromes.json"
# Optional full reference set; fetched on demand, never committed.
AIRPORTS_CSV = REPO_ROOT / "data" / "airports.csv"

# Column names people actually use, mapped to what we need.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "datetime": ("datetime", "event_datetime", "timestamp", "occurred_at", "utc", "utc_datetime"),
    "date": ("date", "event_date", "occurrence_date", "day", "occurred_on"),
    "time": ("time", "event_time", "occurrence_time", "utc_time", "time_utc", "hhmm"),
    "lat": ("lat", "latitude", "lat_deg", "y"),
    "lon": ("lon", "lng", "long", "longitude", "lon_deg", "x"),
    "location": ("location", "place", "aerodrome", "airport", "icao", "icao24_site", "site", "station"),
    "type": ("type", "event_type", "activity", "activity_code", "category", "occurrence_type"),
    "severity": ("severity", "finding_level", "level", "risk", "risk_level"),
    "description": ("description", "summary", "detail", "details", "narrative", "remark", "remarks"),
    "registration": ("registration", "aircraft_registration", "reg", "tail", "tail_number"),
    "callsign": ("callsign", "call_sign", "flight", "flight_number", "flight_no"),
    "altitude_ft": ("altitude_ft", "altitude", "alt_ft", "height_ft"),
}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%Y%m%d")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%H%M", "%H%M%S")

# Decimal degrees with an optional hemisphere letter, e.g. 1.3502N / -84.42
_DD_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*([NSEW])?\s*$", re.IGNORECASE)
# Degrees and decimal minutes, e.g. 1°21.0'N
_DMM_RE = re.compile(
    r"^\s*(-?\d{1,3})\s*[°d:\s]\s*(\d{1,2}(?:\.\d+)?)\s*['m:\s]?\s*([NSEW])?\s*$", re.IGNORECASE
)
# Degrees, minutes, seconds, e.g. 1°21'0.7"N
_DMS_RE = re.compile(
    r"^\s*(-?\d{1,3})\s*[°d:\s]\s*(\d{1,2})\s*['m:\s]\s*(\d{1,2}(?:\.\d+)?)\s*[\"s]?\s*([NSEW])?\s*$",
    re.IGNORECASE,
)

_NEAR_RE = re.compile(r"^\s*near\s+([A-Z]{4})\s*(?:(\d+(?:\.\d+)?)\s*(?:nm|nmi))?\s*$", re.IGNORECASE)
_ICAO_RE = re.compile(r"^[A-Z]{4}$")
_IATA_RE = re.compile(r"^[A-Z]{3}$")


# --------------------------------------------------------------------------- #
# reference data
# --------------------------------------------------------------------------- #

@dataclass
class Place:
    icao: str
    name: str
    lat: float
    lon: float
    source: str = "ourairports"


class LocationResolver:
    """ICAO / IATA / alias lookup over the committed aerodrome reference.

    The full OurAirports file (~86k rows) is used when present at data/airports.csv;
    otherwise the small committed set in db/aerodromes.json is all we know about,
    and anything outside it is refused rather than approximated.
    """

    def __init__(self, aerodromes_path: Path = AERODROMES_JSON, airports_csv: Path = AIRPORTS_CSV) -> None:
        payload = json.loads(aerodromes_path.read_text(encoding="utf-8"))
        self._places: dict[str, Place] = {
            code.upper(): Place(code.upper(), row["name"], float(row["lat"]), float(row["lon"]))
            for code, row in payload["aerodromes"].items()
        }
        self._aliases: dict[str, dict[str, str]] = {
            code.upper(): meta for code, meta in payload.get("aliases", {}).items()
        }
        self._iata: dict[str, str] = {}
        if airports_csv.is_file():
            self._load_full_reference(airports_csv)

    def _load_full_reference(self, path: Path) -> None:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                ident = (row.get("ident") or "").upper()
                icao = (row.get("icao_code") or "").upper()
                code = icao or ident
                if not code or code in self._places:
                    if code and row.get("iata_code"):
                        self._iata[row["iata_code"].upper()] = code
                    continue
                try:
                    self._places[code] = Place(
                        code,
                        row["name"],
                        float(row["latitude_deg"]),
                        float(row["longitude_deg"]),
                        source="ourairports-full",
                    )
                except (TypeError, ValueError):
                    continue
                if row.get("iata_code"):
                    self._iata[row["iata_code"].upper()] = code

    @property
    def known(self) -> int:
        return len(self._places)

    def lookup(self, code: str) -> tuple[Place | None, str, str | None]:
        """Return (place, method, note).

        `method` is explicit — 'icao' for a direct hit, 'alias' for a mapped code,
        'iata' for a three-letter code — because the caller records it in
        provenance and must not infer it by reading the note back.
        """
        code = (code or "").strip().upper()
        if code in self._places:
            return self._places[code], "icao", None
        if code in self._aliases:
            target = self._aliases[code]["target"].upper()
            place = self._places.get(target)
            if place:
                return place, "alias", self._aliases[code].get("reason", f"{code} aliased to {target}")
        if code in self._iata:
            place = self._places.get(self._iata[code])
            if place:
                return place, "iata", f"IATA {code} resolved to {place.icao}"
        return None, "", None


# --------------------------------------------------------------------------- #
# coordinate parsing
# --------------------------------------------------------------------------- #

def parse_coordinate(text: str) -> float | None:
    """Parse one coordinate component in decimal, degrees-minutes or DMS form.

    Returns None when the text is not a coordinate — the caller must not treat
    that as zero.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    match = _DMS_RE.match(raw)
    if match:
        deg, minutes, seconds, hemi = match.groups()
        return _apply_hemisphere(abs(float(deg)) + int(minutes) / 60 + float(seconds) / 3600, deg, hemi)

    match = _DMM_RE.match(raw)
    if match:
        deg, minutes, hemi = match.groups()
        return _apply_hemisphere(abs(float(deg)) + float(minutes) / 60, deg, hemi)

    match = _DD_RE.match(raw)
    if match:
        value = float(match.group(1))
        hemi = match.group(2)
        if hemi and value < 0:
            # "-1.3S" is a contradiction; honour the explicit sign and say nothing
            return value
        return _apply_hemisphere(abs(value), str(value), hemi)

    return None


def _apply_hemisphere(value: float, original: str, hemi: str | None) -> float:
    sign = -1.0 if str(original).strip().startswith("-") else 1.0
    if hemi:
        if hemi.upper() in ("S", "W"):
            sign = -1.0
        elif hemi.upper() in ("N", "E"):
            sign = 1.0
    return round(sign * value, 6)


def parse_coordinate_pair(lat_text: Any, lon_text: Any) -> tuple[float, float] | None:
    """Two separate columns, or one 'lat, lon' / 'lat lon' string."""
    lat = parse_coordinate(str(lat_text) if lat_text is not None else "")
    lon = parse_coordinate(str(lon_text) if lon_text is not None else "")
    if lat is not None and lon is not None:
        return _valid_pair(lat, lon)
    return parse_coordinate_text(f"{lat_text or ''} {lon_text or ''}")


def parse_coordinate_text(text: Any) -> tuple[float, float] | None:
    """Parse 'lat, lon' or 'lat lon' in any format `parse_coordinate` accepts.

    Split on commas first; failing that, try every single-space split and take the
    first where both halves parse. That is what makes `1°21'0.7"N 103°59'38.4"E`
    work without a per-format pattern for the combined case.
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    for parts in ([p.strip() for p in re.split(r"[,;/]", raw)], ):
        if len(parts) == 2:
            lat, lon = parse_coordinate(parts[0]), parse_coordinate(parts[1])
            if lat is not None and lon is not None:
                return _valid_pair(lat, lon)

    tokens = raw.split()
    for split_at in range(1, len(tokens)):
        left, right = " ".join(tokens[:split_at]), " ".join(tokens[split_at:])
        lat, lon = parse_coordinate(left), parse_coordinate(right)
        if lat is not None and lon is not None:
            return _valid_pair(lat, lon)
    return None


def _valid_pair(lat: float, lon: float) -> tuple[float, float] | None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        # The classic "blank cell" coordinate. Never plot it.
        return None
    return lat, lon


def offset_nm(lat: float, lon: float, bearing_deg: float, distance_nm: float) -> tuple[float, float]:
    """Move `distance_nm` from a point along `bearing_deg` (great-circle, spherical)."""
    import math

    radius_nm = 3440.065
    angular = distance_nm / radius_nm
    bearing = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return round(math.degrees(lat2), 6), round(((math.degrees(lon2) + 540) % 360) - 180, 6)


# --------------------------------------------------------------------------- #
# time parsing
# --------------------------------------------------------------------------- #

def parse_when(date_text: Any, time_text: Any = None) -> tuple[datetime | None, str]:
    """Return (utc datetime, time_source). time_source is 'datetime' or 'date_only'."""
    raw_date = str(date_text or "").strip()
    if not raw_date:
        return None, "missing"

    stamp = None
    for fmt in _DATE_FORMATS:
        try:
            stamp = datetime.strptime(raw_date, fmt)
            break
        except ValueError:
            continue
    if stamp is None:
        # Last resort: ISO 8601 with a time component already attached.
        try:
            parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            stamp = parsed
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc), "datetime"
        except ValueError:
            return None, "unparseable"

    raw_time = str(time_text or "").strip()
    for fmt in _TIME_FORMATS:
        if not raw_time:
            break
        try:
            clock = datetime.strptime(raw_time, fmt).time()
            return datetime.combine(stamp.date(), clock, tzinfo=timezone.utc), "datetime"
        except ValueError:
            continue

    return stamp.replace(tzinfo=timezone.utc), "date_only"


# --------------------------------------------------------------------------- #
# file parsing
# --------------------------------------------------------------------------- #

def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical_columns(header: Sequence[str]) -> dict[str, int]:
    """Map our field names to column indexes, using the alias table."""
    lowered = [h.strip().lower().replace(" ", "_") for h in header]
    mapping: dict[str, int] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                mapping[field_name] = lowered.index(alias)
                break
    return mapping


def parse_table(text: str, filename: str = "") -> list[dict[str, str]]:
    """Read CSV (or TSV) text into rows keyed by our canonical field names."""
    sample = text[:4096]
    delimiter = "\t" if filename.lower().endswith((".tsv", ".txt")) or sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return []

    columns = _canonical_columns(header)
    rows: list[dict[str, str]] = []
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            continue
        row: dict[str, str] = {}
        for name, index in columns.items():
            row[name] = _clean(raw[index]) if index < len(raw) else ""
        rows.append(row)
    return rows


def parse_upload(content: bytes, filename: str) -> list[dict[str, str]]:
    """Decode an uploaded file. XLSX is refused with a clear message, not a crash."""
    lowered = filename.lower()
    if lowered.endswith((".xlsx", ".xls")):
        raise ValueError(
            "Excel files are not read yet — save the sheet as CSV and upload that. "
            "(Adding openpyxl is a one-line change once the column set settles.)"
        )
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 decodes anything
        raise ValueError("file is not readable text")
    return parse_table(text, filename)


# --------------------------------------------------------------------------- #
# rows -> events
# --------------------------------------------------------------------------- #

@dataclass
class ParsedRow:
    index: int
    raw: dict[str, str]
    event: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "ok": not self.errors,
            "errors": self.errors,
            "raw": self.raw,
            "event": self.event,
        }


def resolve_position(
    row: dict[str, str], resolver: LocationResolver
) -> tuple[float | None, float | None, str, str | None, str | None, str | None, list[str]]:
    """Work out where a row belongs.

    Returns (lat, lon, method, icao, place_name, note, errors). `note` is a
    sentence for provenance and is None when nothing was inferred.
    """
    errors: list[str] = []

    # 1. explicit lat/lon columns, or a coordinate string in those columns
    pair = parse_coordinate_pair(row.get("lat"), row.get("lon"))
    if pair:
        return pair[0], pair[1], "coordinates", None, None, None, errors

    location = (row.get("location") or "").strip()
    if not location:
        errors.append("no position: no lat/lon and no location")
        return None, None, "", None, None, None, errors

    # 2. a coordinate pair pasted into the location column — the common case for a
    #    spreadsheet exported from somewhere else
    pair = parse_coordinate_text(location)
    if pair:
        return pair[0], pair[1], "coordinates", None, None, None, errors

    # 3. 'near WSSS' / 'near WSSS 5NM'
    near = _NEAR_RE.match(location)
    if near:
        code, nm = near.group(1).upper(), near.group(2)
        place, method, note = resolver.lookup(code)
        if place is None:
            errors.append(f"location '{location}': unknown aerodrome {code}")
            return None, None, "", None, None, None, errors
        if nm:
            # A radial position needs a bearing; without one we cannot say which
            # side of the field the event was on, so it is refused rather than
            # snapped to the airport centre.
            errors.append(
                f"location '{location}': 'near {code} {nm} NM' needs a bearing — supply coordinates or a bearing"
            )
            return None, None, "", None, None, None, errors
        return place.lat, place.lon, "near", place.icao, place.name, f"{code} -> {place.icao}", errors

    # 4. an aerodrome code
    place, method, note = resolver.lookup(location)
    if place is None:
        errors.append(f"location '{location}': not a known ICAO/IATA code and not a coordinate")
        return None, None, "", None, None, None, errors

    return place.lat, place.lon, method, place.icao, place.name, note, errors


def build_events(rows: Iterable[dict[str, str]], resolver: LocationResolver | None = None) -> list[ParsedRow]:
    """Turn parsed rows into events, collecting per-row errors instead of raising."""
    resolver = resolver or LocationResolver()
    parsed: list[ParsedRow] = []

    for index, row in enumerate(rows, start=1):
        item = ParsedRow(index=index, raw=row)

        lat, lon, method, icao, place_name, note, errors = resolve_position(row, resolver)
        item.errors.extend(errors)

        when, time_source = parse_when(row.get("datetime") or row.get("date"), row.get("time"))
        if when is None:
            item.errors.append(
                "no readable date" if time_source == "missing" else f"date '{row.get('date')}' is not a format we read"
            )

        if item.errors:
            parsed.append(item)
            continue

        event_type = (row.get("type") or "other").strip()
        severity = (row.get("severity") or "").strip()
        event = {
            "event_time": when,
            "event_type": event_type,
            "severity": severity or "unknown",
            "lat": lat,
            "lon": lon,
            "place_icao": icao or (row.get("location") or "").upper() or None,
            "place_name": place_name,
            "altitude_ft": _maybe_int(row.get("altitude_ft")),
            "callsigns": [row["callsign"]] if row.get("callsign") else [],
            "registration": row.get("registration") or None,
            "description": row.get("description") or "",
            "time_source": time_source,
            "position_source": method,
            "position_note": note,
        }
        item.event = event
        parsed.append(item)

    return parsed


def _maybe_int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def to_store_events(parsed: Sequence[ParsedRow], filename: str, source: str = "upload") -> list[dict[str, Any]]:
    """Shape accepted rows for store.insert_events, with an idempotency key.

    external_ref is a hash of the row's own content, so re-uploading the same file
    updates nothing and duplicates nothing.
    """
    import hashlib

    events: list[dict[str, Any]] = []
    for item in parsed:
        if item.errors or not item.event:
            continue
        event = item.event
        fingerprint = "|".join(
            str(event.get(key, ""))
            for key in ("event_time", "event_type", "lat", "lon", "description")
        )
        digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
        notes = [f"uploaded file {filename or 'unnamed'}", f"position from {event['position_source']}"]
        if event.get("position_note"):
            notes.append(str(event["position_note"]))
        if event.get("time_source") == "date_only":
            notes.append("file carried a date but no time; recorded at 00:00 UTC")
        events.append(
            {
                "event_time": event["event_time"],
                "event_type": event["event_type"],
                "severity": event["severity"],
                "lat": event["lat"],
                "lon": event["lon"],
                "place_icao": event["place_icao"],
                "place_name": event["place_name"],
                "altitude_ft": event["altitude_ft"],
                "callsigns": event["callsigns"],
                "icao24": [],
                "registration": event["registration"],
                "metrics": {},
                "source": source,
                "provenance": "; ".join(notes),
                "external_ref": f"upload:{digest}",
                "raw": {"file": filename, "row": item.raw},
            }
        )
    return events
