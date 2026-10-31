"""Let MITRE's OpenARIA do the separation maths, and keep only what it can prove.

Our own detectors judge the traffic in front of the map: they see a poll, compare
pairs, and forget. OpenARIA is the opposite kind of instrument — a batch analyser that
walks whole tracks, computes closest point of approach for every pair, scores the
encounter, and writes the arithmetic out as JSON. Running it over the position history
we already store catches conflicts that no single poll ever contained.

Three deliberate restrictions, matching the rest of this project:

* **Observed, not predicted.** OpenARIA is fed only positions we actually recorded.
  Nothing here extrapolates a track to manufacture an event.
* **Same door as everything else.** Rows are built to the shape `store.normalise_event`
  accepts and inserted through `store.insert_events`, so an OpenARIA event passes the
  same validation as an upload or a report — no side entrance into the table.
* **Idempotent.** `external_ref` is `openaria:<uniqueId>`, and that id is a hash of the
  pair and time, so re-running yesterday's window rewrites nothing.

What it does *not* do: unstable approaches, go-arounds, runway incursions. OpenARIA
only knows airborne encounters. Those stay with `detect.py`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import detect

# Where the uber-jar lives. It is a 49 MB binary and is deliberately not in git;
# point OPENARIA_JAR at it, or drop it in vendor/.
JAR_ENV = "OPENARIA_JAR"
DEFAULT_JAR = Path(__file__).resolve().parents[1] / "vendor" / "open-aria-uber.jar"
CONFIG_FILE = Path(__file__).resolve().with_name("openaria_config.yaml")

ENGINE_VERSION = "0.5.0"

# The minima this project already uses: 3 NM / 1,000 ft inside controlled airspace.
HIGH_LATERAL_NM, HIGH_VERTICAL_FT = 3.0, 1000.0
# A second, wider ring — worth a look, not an incident.
MEDIUM_LATERAL_NM, MEDIUM_VERTICAL_FT = 5.0, 1500.0


# --------------------------------------------------------------------------- #
# CSV out: our tracks in the format OpenARIA reads
# --------------------------------------------------------------------------- #

def csv_line(track: Mapping[str, Any]) -> str:
    """One position as an OpenARIA CSV row.

    Column order is fixed by the format: partition, subpartition, timestamp,
    vehicle_id, latitude, longitude, altitude, then any custom columns. We put the
    callsign in column 8 — OpenARIA ignores it, but the raw track it echoes back into
    the event JSON keeps the whole line, so the callsign survives the round trip.
    """
    lat = track.get("lat")
    lon = track.get("lon")
    alt = track.get("altitude_ft")
    if lat is None or lon is None or alt is None:
        raise ValueError(f"track needs lat/lon/altitude (got {lat!r}, {lon!r}, {alt!r})")

    when = track["observed_at"]
    if isinstance(when, datetime):
        stamp = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        stamp = str(when)

    callsign = (track.get("callsign") or "").strip()
    return ",".join(
        [
            str(track.get("partition") or "SG"),
            str(track.get("subpartition") or "0"),
            stamp,
            str(track["icao24"]).lower(),
            f"{float(lat):.5f}",
            f"{float(lon):.5f}",
            str(int(round(float(alt)))),
            callsign,
        ]
    )


def export_tracks(conn: Any, *, since: datetime, until: datetime, path: Path) -> int:
    """Write the stored positions in a window out as OpenARIA CSV. Returns row count.

    Ground traffic is left out. OpenARIA reasons about airborne pairs, and a rolling-out
    aircraft reported at 0 ft while flagged airborne becomes an aircraft "at" the runway
    threshold — which is how a normal arrival sequence turns into a reported encounter.
    """
    query = """
        SELECT icao24, observed_at, lat, lon, altitude_ft, callsign
          FROM aircraft_track
         WHERE observed_at >= %s AND observed_at < %s
           AND lat IS NOT NULL AND lon IS NOT NULL AND altitude_ft IS NOT NULL
           AND on_ground = false
           AND altitude_ft > %s
         ORDER BY observed_at, icao24
    """
    written = 0
    with conn.cursor() as cur:
        cur.execute(query, (since, until, detect.GROUND_FLOOR_FT))
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for icao24, observed_at, lat, lon, altitude_ft, callsign in cur:
                handle.write(
                    csv_line(
                        {
                            "icao24": icao24,
                            "observed_at": observed_at,
                            "lat": lat,
                            "lon": lon,
                            "altitude_ft": altitude_ft,
                            "callsign": callsign,
                        }
                    )
                    + "\n"
                )
                written += 1
    return written


# --------------------------------------------------------------------------- #
# JSON in: reading what OpenARIA wrote
# --------------------------------------------------------------------------- #

def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield every event object in one file.

    OpenARIA writes one file per event, but a file is not one JSON document: the same
    encounter can be emitted more than once, giving concatenated objects. `json.load`
    fails on that with "Extra data" — so decode piece by piece and let dedupe handle
    the repeats.
    """
    raw = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        payload, index = decoder.raw_decode(raw, index)
        if isinstance(payload, dict):
            yield payload


def load_events(directory: Path) -> list[dict[str, Any]]:
    """All events in a run directory, one per uniqueId, keeping the highest score."""
    best: dict[str, dict[str, Any]] = {}
    for file_path in sorted(Path(directory).glob("*.json")):
        for payload in iter_events(file_path):
            key = str(payload.get("uniqueId") or "")
            if not key:
                continue
            previous = best.get(key)
            if previous is None or float(payload.get("eventScore") or 0) > float(previous.get("eventScore") or 0):
                payload.setdefault("_sourceFile", file_path.name)
                best[key] = payload
    return list(best.values())


# --------------------------------------------------------------------------- #
# Translation: OpenARIA's opinion of an encounter -> one row of our store
# --------------------------------------------------------------------------- #

def severity_for(lateral_nm: float, vertical_ft: float) -> str:
    """How bad, on this project's existing minima rather than on OpenARIA's score.

    The score is unbounded and tuned for a different airspace; the 3 NM / 1,000 ft
    rule the rest of the map already uses is the one an operator can check by hand.
    """
    if lateral_nm <= HIGH_LATERAL_NM and abs(vertical_ft) <= HIGH_VERTICAL_FT:
        return "high"
    if lateral_nm <= MEDIUM_LATERAL_NM or abs(vertical_ft) <= MEDIUM_VERTICAL_FT:
        return "medium"
    return "low"


def external_ref(unique_id: str) -> str:
    """Stable key for the unique index: the same encounter is only ever one row."""
    return f"openaria:{unique_id}"


def _track_fields(payload: Mapping[str, Any], which: str) -> tuple[str | None, str | None]:
    """Recover (icao24, callsign) for one aircraft of the pair.

    OpenARIA hashes the vehicle id into `uniqueId`, but keeps the original id in
    `trackId` and echoes the raw CSV line it was given into the track array — column 4
    is our icao24 and column 8 our callsign.
    """
    aircraft = payload.get(f"aircraft_{which}") or {}
    icao24 = str(aircraft.get("trackId") or "").strip().lower() or None

    callsign = None
    track = payload.get(f"track{which}") or []
    if track and isinstance(track[0], str):
        parts = track[0].split(",")
        if len(parts) >= 8 and parts[7].strip():
            callsign = parts[7].strip()
        if icao24 is None and len(parts) >= 4:
            icao24 = parts[3].strip().lower() or None
    if callsign in ("", "UNKNOWN"):
        candidate = str(aircraft.get("callsign") or "").strip()
        callsign = candidate if candidate and candidate.upper() != "UNKNOWN" else None
    return icao24, callsign


def event_from_openaria(
    payload: Mapping[str, Any],
    aerodromes: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """One OpenARIA event as a row the store will accept, or None if unusable.

    Returns None rather than a half-built row: a lat/lon that cannot be read is not a
    warning, it is a permanently wrong dot on the map.
    """
    try:
        lat = float(payload["latitude"])
        lon = float(payload["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    try:
        when = datetime.fromtimestamp(int(payload["eventEpochMsTime"]) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None

    # The separation to report is the closest lateral approach, which OpenARIA also
    # annotates with the vertical gap at that moment. `atEventTime` is the fallback
    # for the rare payload that omits it.
    cpa = payload.get("atClosestLateral") or payload.get("atEventTime") or {}
    try:
        lateral_nm = round(float(cpa.get("trueLateralNm")), 3)
        vertical_ft = round(float(cpa.get("trueVerticalFt")), 0)
    except (TypeError, ValueError):
        return None

    icao_a, call_a = _track_fields(payload, "0")
    icao_b, call_b = _track_fields(payload, "1")
    callsigns = [c for c in (call_a, call_b) if c]
    icao24 = [c for c in (icao_a, icao_b) if c]

    score = float(payload.get("eventScore") or 0.0)
    conflict_angle = str(payload.get("conflictAngle") or "UNKNOWN")
    altitude = None
    for key in ("aircraft_0", "aircraft_1"):
        value = (payload.get(key) or {}).get("altitudeInFeet")
        if value is not None:
            altitude = int(value)
            break

    place_icao, place_name = (None, None)
    if aerodromes:
        import promote  # noqa: PLC0415 — local import keeps the pure functions import-light

        place_icao, place_name = promote._nearest_aerodrome(lat, lon, aerodromes)  # noqa: SLF001

    pair = " / ".join(callsigns) if callsigns else " / ".join(icao24) or "two aircraft"
    metrics = {
        "engine": f"openaria-{ENGINE_VERSION}",
        "score": round(score, 2),
        "lateral_nm": lateral_nm,
        "vertical_ft": vertical_ft,
        "conflict_angle": conflict_angle,
        "time_to_cpa_s": round(float(payload.get("timeToCpaInMilliSec") or 0) / 1000, 1),
        "closure_kt": cpa.get("lateralClosureRateKt"),
        "vertical_rate_fpm": cpa.get("vertClosureRateFtPerMin"),
        "altitude_ft": altitude,
        "track_samples": [len(payload.get("track0") or []), len(payload.get("track1") or [])],
        "map_png": payload.get("_mapFile"),
    }

    provenance = (
        f"MITRE OpenARIA {ENGINE_VERSION} analysed the stored position history and found "
        f"a closest approach of {lateral_nm} NM and {abs(vertical_ft):.0f} ft "
        f"({conflict_angle.lower()} geometry, score {score:.2f}) between {pair}. "
        "Positions are OpenSky state vectors sampled about every 15 s, so the lateral "
        "figure carries sampling error of roughly a tenth of a nautical mile."
    )

    return {
        "event_time": when,
        "event_type": "loss_of_separation",
        "severity": severity_for(lateral_nm, vertical_ft),
        "lat": lat,
        "lon": lon,
        "place_icao": place_icao,
        "place_name": place_name,
        "altitude_ft": altitude,
        "callsigns": callsigns,
        "icao24": icao24,
        "metrics": metrics,
        "source": "derived",
        "provenance": provenance,
        "external_ref": external_ref(str(payload.get("uniqueId") or "")),
        "raw": {k: v for k, v in payload.items() if not k.startswith("_")},
    }


def events_from_run(directory: Path, aerodromes: Iterable[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Translate a whole run directory, dropping payloads we could not turn into rows."""
    rows = []
    for payload in load_events(directory):
        row = event_from_openaria(payload, aerodromes)
        if row is not None:
            rows.append(row)
    return rows


def encounter_tracks(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The two tracks OpenARIA walked, as drawable positions.

    It echoes back the exact CSV lines it was given, so the encounter the map draws is
    the data that produced the event — not a re-request to a live feed that has since
    moved on. Returns None when the payload carries no usable track.
    """
    tracks: list[dict[str, Any]] = []
    for which in ("0", "1"):
        points: list[list[float]] = []
        callsign = None
        icao24 = None
        for line in payload.get(f"track{which}") or []:
            if not isinstance(line, str):
                continue
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                lat = float(parts[4])
                lon = float(parts[5])
            except (TypeError, ValueError):
                continue
            altitude = None
            try:
                altitude = float(parts[6])
            except (TypeError, ValueError):
                pass
            points.append([lon, lat, altitude if altitude is not None else 0.0])
            if icao24 is None and len(parts) >= 4:
                icao24 = parts[3].strip().lower() or None
            if callsign is None and len(parts) >= 8 and parts[7].strip():
                callsign = parts[7].strip()

        if len(points) >= 2:
            tracks.append(
                {"icao24": icao24, "callsign": callsign, "points": points, "count": len(points)}
            )

    if len(tracks) < 2:
        return None

    cpa = payload.get("atClosestLateral") or payload.get("atEventTime") or {}
    return {
        "tracks": tracks,
        "cpa": {
            "lat": payload.get("latitude"),
            "lon": payload.get("longitude"),
            "lateral_nm": cpa.get("trueLateralNm"),
            "vertical_ft": cpa.get("trueVerticalFt"),
        },
    }


# --------------------------------------------------------------------------- #
# Running the engine
# --------------------------------------------------------------------------- #

def jar_path() -> Path:
    return Path(os.environ.get(JAR_ENV) or DEFAULT_JAR)


def run_engine(csv_path: Path, workdir: Path, *, config: Path | None = None, heap: str = "700m") -> dict[str, Any]:
    """Run the uber-jar over one CSV. Returns what it said, never raises on its stderr."""
    jar = jar_path()
    if not jar.exists():
        raise FileNotFoundError(
            f"OpenARIA jar not found at {jar}. Download open-aria-uber.jar from "
            "https://github.com/mitre-public/open-aria/releases and set OPENARIA_JAR."
        )
    cfg = Path(config or CONFIG_FILE)
    if not cfg.exists():
        raise FileNotFoundError(f"OpenARIA config not found at {cfg}")

    workdir.mkdir(parents=True, exist_ok=True)
    command = [
        "java",
        f"-Xmx{heap}",
        "-cp",
        str(jar),
        "org.mitre.openaria.RunAirborneOnFile",
        "-f",
        str(csv_path),
        "-c",
        str(cfg),
    ]
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        command,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def ingest(conn: Any, rows: Iterable[dict[str, Any]]) -> int:
    """Insert through the same validation door every other producer uses."""
    import store  # noqa: PLC0415

    return store.insert_events(conn, rows)


def _store_is_ready(conn: Any) -> bool:
    """Does the event table already exist?

    A producer must never create it: `db/001_safety_event.sql` starts with DROP TABLE,
    so calling `store.apply_schema` here would silently erase every event already on
    the map. If the table is missing, say so and stop.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.safety_event') IS NOT NULL")
        row = cur.fetchone()
    return bool(row and row[0])


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import timedelta

    parser = argparse.ArgumentParser(description="Run MITRE OpenARIA over stored tracks and store what it finds.")
    parser.add_argument("--hours", type=float, default=24.0, help="how far back to analyse (default 24)")
    parser.add_argument(
        "--overlap-hours",
        type=float,
        default=2.0,
        help=(
            "extend the window back by this much so an encounter straddling the previous "
            "run's boundary is seen twice rather than missed (default 2)"
        ),
    )
    parser.add_argument("--since", help="ISO timestamp; overrides --hours")
    parser.add_argument("--until", help="ISO timestamp; defaults to now")
    parser.add_argument("--workdir", default="/tmp/openaria-run", help="scratch directory for CSV and output")
    parser.add_argument("--dry-run", action="store_true", help="analyse and print, but write nothing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    import store  # noqa: PLC0415
    import promote  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    until = datetime.fromisoformat(args.until) if args.until else now
    # Overlap is cheaper than a missed encounter: the engine sees the same boundary twice,
    # and the unique index on (source, external_ref) turns the repeat into a no-op.
    hours = args.hours + max(0.0, args.overlap_hours)
    since = datetime.fromisoformat(args.since) if args.since else until - timedelta(hours=hours)

    workdir = Path(args.workdir)
    for stale in ("detectedEvents", "eventMaps"):
        target = workdir / stale
        if target.exists():
            import shutil  # noqa: PLC0415

            shutil.rmtree(target)

    with store.connect() as conn:
        if not _store_is_ready(conn):
            print(
                "the safety_event table is missing; apply db/001_safety_event.sql by hand first. "
                "This pass never recreates the store — that file drops the table.",
                file=sys.stderr,
            )
            return 2
        csv_path = workdir / "tracks.csv"
        workdir.mkdir(parents=True, exist_ok=True)
        rows = export_tracks(conn, since=since, until=until, path=csv_path)
        if not args.quiet:
            print(f"exported {rows} positions ({since:%Y-%m-%d %H:%M} -> {until:%Y-%m-%d %H:%M} UTC)")
        if rows == 0:
            print("nothing to analyse: no stored positions in that window")
            return 0

        result = run_engine(csv_path, workdir)
        if result["returncode"] != 0:
            print(f"OpenARIA exited {result['returncode']}\n{result['stderr']}", file=sys.stderr)
            return result["returncode"]

        events = events_from_run(workdir / "detectedEvents", promote.load_aerodromes())
        if not args.quiet:
            print(f"OpenARIA reported {len(events)} encounter(s)")
            for event in events:
                m = event["metrics"]
                print(
                    f"  {event['event_time']:%Y-%m-%d %H:%M:%S}Z  "
                    f"{m['lateral_nm']:.2f} NM / {m['vertical_ft']:.0f} ft  "
                    f"score {m['score']:.2f}  {', '.join(event['callsigns']) or 'unknown'}"
                )
        if args.dry_run:
            print("dry run: nothing written")
            return 0

        written = ingest(conn, events)
        if not args.quiet:
            print(f"stored {written} new event(s) ({len(events) - written} already known)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
