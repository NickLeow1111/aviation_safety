"""Seed `safety_event` from the existing synthetic occurrences.

Run:  python seed_events.py            # add to whatever is there (idempotent)
      python seed_events.py --reset    # drop and rebuild the table first

What this script will not do is invent facts quietly. Two fields in
DM_TBL_SRG_OCCURRENCES cannot be drawn and have to be derived:

  * coordinates  — the table stores `Location` as free text ('WSSS'). Here that
                   code is resolved against db/aerodromes.json, whose values come
                   from OurAirports, not from me. Unknown codes are skipped and
                   reported, never guessed.
  * time-of-day  — the table stores `Occurrence_Date` only. A dot with no hour
                   cannot be filtered by hour, so each row is given a
                   deterministic hour derived from its own Occurrence_ID. The
                   same row always lands at the same time; nobody mistakes it for
                   recorded data, because `provenance` says so on every row.

Points are also nudged by at most 0.02° so a year of reports at one aerodrome
does not collapse into a single dot — again deterministic, again disclosed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

AGENT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = AGENT_ROOT.parent
sys.path.insert(0, str(AGENT_ROOT))

import store  # noqa: E402  (after sys.path juggling, by design)

OCCURRENCES_SQL = REPO_ROOT / "SQL" / "DM_TBL_SRG_OCCURRENCES_SyntheticData.sql"
AERODROMES_JSON = REPO_ROOT / "db" / "aerodromes.json"

# The regulator's own activity codes, as they appear in the source table.
ACTIVITY_TO_TYPE = {
    "bird strike": "bird_strike",
    "runway incursion": "runway_incursion",
    "taxi error investigation": "taxi_error",
    "external damage": "external_damage",
    "fuel spill": "fuel_spill",
    "accident investigation": "accident_investigation",
}

# Finding_Level is a finding severity, not an event severity, but it is the only
# graded signal in the row and mapping it is better than emitting everything as
# 'unknown'. Level 1 is the most serious finding in this scheme.
LEVEL_TO_SEVERITY = {"level 1": "critical", "level 2": "high", "level 3": "medium", "obs": "low"}

_INSERT_RE = re.compile(r"INSERT INTO\s+\S+\s*\(([^)]*)\)\s*VALUES\s*\(", re.IGNORECASE)


def _split_sql_tuple(text: str) -> list[str]:
    """Split the inside of a VALUES (...) tuple into raw field strings.

    Handles quoted strings (with '' escapes), NULL, and numbers. Deliberately
    small: the source file is machine-generated with single-row INSERTs.
    """
    fields: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quotes:
            if ch == "'" and i + 1 < len(text) and text[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            if ch == "'":
                in_quotes = False
            else:
                buf.append(ch)
        else:
            if ch == "'":
                in_quotes = True
            elif ch == ",":
                fields.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        i += 1
    fields.append("".join(buf).strip())
    return fields


def parse_occurrences(path: Path = OCCURRENCES_SQL) -> list[dict[str, Any]]:
    """Read the synthetic INSERT statements into plain dicts."""
    text = path.read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = []

    for line in text.splitlines():
        match = _INSERT_RE.search(line)
        if not match:
            continue
        columns = [c.strip().strip("[]") for c in match.group(1).split(",")]
        body_start = match.end()
        body_end = line.rfind(")")
        if body_end <= body_start:
            continue
        fields = _split_sql_tuple(line[body_start:body_end])
        if len(fields) != len(columns):
            continue
        rows.append({col: (None if val.upper() == "NULL" else val) for col, val in zip(columns, fields)})
    return rows


def _digest(seed: str, modulo: int) -> int:
    return int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % modulo


def load_aerodromes(path: Path = AERODROMES_JSON) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["aerodromes"], payload.get("aliases", {})


def resolve_place(code: str, aerodromes: dict[str, Any], aliases: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """ICAO code -> aerodrome row. Returns (row, alias_note)."""
    code = (code or "").strip().upper()
    if code in aerodromes:
        return aerodromes[code], None
    alias = aliases.get(code)
    if alias and alias.get("target") in aerodromes:
        return aerodromes[alias["target"]], alias.get("reason", f"{code} aliased to {alias['target']}")
    return None, None


def to_event(row: dict[str, Any], aerodromes: dict[str, Any], aliases: dict[str, Any]) -> dict[str, Any] | None:
    """One source row -> one map event, or None if it cannot be placed honestly."""
    ref = row.get("Occurrence_ID") or ""
    raw_date = row.get("Occurrence_Date")
    if not ref or not raw_date:
        return None

    aerodrome, alias_note = resolve_place(row.get("Location") or "", aerodromes, aliases)
    if aerodrome is None:
        return None

    day = datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # Deterministic time-of-day and jitter: same row, same point, every run.
    when = day + timedelta(minutes=_digest(ref + ":time", 24 * 60 // 5) * 5)
    lat = aerodrome["lat"] + (_digest(ref + ":lat", 41) - 20) / 1000.0
    lon = aerodrome["lon"] + (_digest(ref + ":lon", 41) - 20) / 1000.0

    activity = (row.get("Activity_Code") or "").strip().lower()
    event_type = ACTIVITY_TO_TYPE.get(activity, "other")
    finding = (row.get("Finding_Level") or "").strip().lower()
    severity = LEVEL_TO_SEVERITY.get(finding, "unknown")

    notes = [
        f"coordinates from OurAirports reference point for {row.get('Location')} "
        f"({aerodrome['name']}), nudged <=0.02 deg for legibility",
        "time-of-day derived from Occurrence_ID (source has date only)",
        f"severity mapped from Finding_Level '{row.get('Finding_Level')}'",
    ]
    if alias_note:
        notes.append(alias_note)

    return {
        "event_time": when,
        "event_type": event_type,
        "severity": severity,
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "place_icao": (row.get("Location") or "").upper() or None,
        "place_name": aerodrome["name"],
        "altitude_ft": None,
        "callsigns": [],
        "icao24": [],
        "registration": row.get("Aircraft_Registration"),
        "metrics": {},
        "source": "report",
        "provenance": "DM_TBL_SRG_OCCURRENCES " + "; ".join(notes),
        "external_ref": ref,
        "raw": row,
    }


def build_events(path: Path = OCCURRENCES_SQL) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (events, unplaceable references)."""
    aerodromes, aliases = load_aerodromes()
    events: list[dict[str, Any]] = []
    skipped: list[str] = []
    for row in parse_occurrences(path):
        event = to_event(row, aerodromes, aliases)
        if event is None:
            skipped.append(f"{row.get('Occurrence_ID')} @ {row.get('Location')}")
        else:
            events.append(event)
    return events, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed safety_event from the synthetic occurrences.")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (default: local socket)")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate safety_event first")
    args = parser.parse_args(argv)

    events, skipped = build_events()
    if skipped:
        print(f"unplaceable rows skipped ({len(skipped)}): {', '.join(skipped)}")

    with store.connect(args.dsn) as conn:
        if args.reset:
            store.apply_schema(conn)
            print("schema applied (safety_event recreated)")
        written = store.insert_events(conn, events)
        total = store.query_events(conn, limit=1)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM safety_event")
            count = cur.fetchone()[0]
        by_type = store.count_by_type(conn, limit=5000)

    print(f"events built: {len(events)} | written: {written} | rows now in table: {count}")
    for name, n in by_type.items():
        print(f"  {name:24} {n}")
    if total and count:
        with store.connect(args.dsn) as conn:
            sample = store.query_events(conn, limit=1)
        if sample:
            row = sample[0]
            print(
                "newest:",
                row["event_time"].isoformat(),
                row["event_type"],
                row["place_icao"],
                f"({row['lat']:.4f},{row['lon']:.4f})",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
