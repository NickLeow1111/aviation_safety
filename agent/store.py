"""Event store for the safety map.

The rule the rest of this project already runs on: **the database decides facts,
the model decides wording.** This module is the only place that talks to
Postgres, so every filter the map, an upload or the agent can express becomes a
parameterised query here. Nothing downstream gets to filter rows in its head and
call the result a fact.

Layout
------
- `safety_event` holds one row per event (see db/001_safety_event.sql).
- `source` says who is making the claim: a filed report, an uploaded file, or a
  detection derived from tracks.
- `provenance` says how the claim was reached, in a sentence, including any
  field that was derived rather than received. A dot on the map can always be
  questioned.

The psycopg import is deferred so the pure functions below can be unit-tested on
a machine with no driver installed — the same shape as tests/test_nl2sql.py.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

# Local POC default: Unix socket, peer auth, no password anywhere.
DEFAULT_DSN = "postgresql:///safetyintel?host=/var/run/postgresql"
SCHEMA_FILE = Path(__file__).resolve().parents[1] / "db" / "001_safety_event.sql"

SOURCES = ("report", "upload", "derived")

# Filters the map offers that are not column values. "aria" is not a `source`: it is the
# subset of derived events an engine computed over the whole track history, which is what
# the ARIA tag already means in the table.
VIRTUAL_SOURCES = {"aria": "source = 'derived' AND metrics->>'engine' IS NOT NULL"}
SOURCE_FILTERS = SOURCES + tuple(VIRTUAL_SOURCES)
SEVERITIES = ("critical", "high", "medium", "low", "unknown")

# Canonical event types the map knows how to draw and colour. A producer may use
# a type outside this list — it becomes a plain marker rather than an error,
# because refusing to plot a real event is worse than plotting it generically.
EVENT_TYPES = (
    "loss_of_separation",
    "runway_incursion",
    "go_around",
    "bird_strike",
    "taxi_error",
    "external_damage",
    "fuel_spill",
    "accident_investigation",
    "other",
)

_COLUMNS = (
    "id, event_time, event_type, severity, lat, lon, place_icao, place_name, "
    "altitude_ft, callsigns, icao24, registration, metrics, source, provenance, "
    "external_ref, ingested_at, detector, invalidated_at, invalidation_reason"
)

# A snapshot detector and a trajectory engine are not the same witness, and comparing
# their yields is the point of keeping them apart. The column is derived once, at insert,
# so a stored row never has to be re-interpreted by whoever reads it later.
DETECTORS = ("snapshot_separation", "snapshot_motion", "openaria", "reported", "unknown")


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #

def dsn() -> str:
    """Where Postgres is. Local socket by default; DATABASE_URL wins if set."""
    return os.environ.get("DATABASE_URL") or os.environ.get("SAFETY_EVENT_DSN") or DEFAULT_DSN


@contextmanager
def connect(dsn_str: str | None = None) -> Iterator[Any]:
    """Open a connection, commit on success, roll back on failure."""
    try:
        import psycopg  # noqa: PLC0415 — deferred so pure functions stay importable
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "psycopg is not installed. `pip install 'psycopg[binary]'` in the agent venv."
        ) from exc

    conn = psycopg.connect(dsn_str or dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_schema(conn: Any, schema_file: Path | None = None) -> None:
    """Create (or recreate) `safety_event`. DESTRUCTIVE — the file drops the table.

    Kept explicit and separate from `connect()` so nobody loses data by importing
    a module.
    """
    sql = (schema_file or SCHEMA_FILE).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are treated as UTC, not as local time.

    The host clock is UTC and the source data has no zone at all; assuming local
    time would shift every dot by the offset and nobody would see it.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _slug(value: str) -> str:
    return "_".join(
        part for part in "".join(ch if ch.isalnum() else "_" for ch in value.lower()).split("_") if part
    )


def normalise_event(event: dict[str, Any]) -> dict[str, Any]:
    """Coerce one event into something the table will accept.

    Raises ValueError rather than writing a half-valid row: a bad coordinate in a
    map database is not a warning, it is a permanently wrong dot.
    """
    try:
        lat = float(event["lat"])
        lon = float(event["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"event needs numeric lat/lon (got {event.get('lat')!r}, {event.get('lon')!r})") from exc
    if not -90 <= lat <= 90:
        raise ValueError(f"lat out of range: {lat}")
    if not -180 <= lon <= 180:
        raise ValueError(f"lon out of range: {lon}")

    when = event.get("event_time")
    if not isinstance(when, datetime):
        raise ValueError(f"event_time must be a datetime (got {type(when).__name__})")

    event_type = _slug(str(event.get("event_type") or "other")) or "other"
    severity = _slug(str(event.get("severity") or "unknown")) or "unknown"
    if severity not in SEVERITIES:
        severity = "unknown"
    source = str(event.get("source") or "").strip()
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES} (got {source!r})")

    metrics = event.get("metrics") or {}
    detector = _detector_for(event, metrics)

    def _list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value if item]

    return {
        "event_time": _as_utc(when),
        "event_type": event_type,
        "severity": severity,
        "lat": lat,
        "lon": lon,
        "place_icao": (str(event["place_icao"]).upper() if event.get("place_icao") else None),
        "place_name": event.get("place_name") or None,
        "altitude_ft": int(event["altitude_ft"]) if event.get("altitude_ft") is not None else None,
        "callsigns": _list(event.get("callsigns")),
        "icao24": [c.lower() for c in _list(event.get("icao24"))],
        "registration": event.get("registration") or None,
        "metrics": metrics,
        "source": source,
        "detector": detector,
        "provenance": str(event.get("provenance") or "unspecified"),
        "external_ref": event.get("external_ref") or None,
        "raw": event.get("raw") or {},
    }




def _detector_for(event: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Which witness produced this row.

    Recorded once, at insert, rather than re-derived by every reader: the whole value of
    the split is comparing what one detector catches against another, and that comparison
    is worthless if each consumer guesses differently.
    """
    explicit = str(event.get("detector") or "").strip()
    if explicit:
        return explicit if explicit in DETECTORS else "unknown"

    marker = str(metrics.get("detector") or "").strip()
    if marker == "snapshot_separation":
        return "snapshot_separation"
    if marker == "track_motion":
        return "snapshot_motion"

    engine = str(metrics.get("engine") or "").strip().lower()
    if engine.startswith("openaria"):
        return "openaria"

    source = str(event.get("source") or "").strip()
    if source in {"report", "upload"}:
        return "reported"
    return "unknown"

# --------------------------------------------------------------------------- #
# query building (pure — this is what the tests exercise)
# --------------------------------------------------------------------------- #

def _validate_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must be (min_lat, min_lon, max_lat, max_lon)")
    min_lat, min_lon, max_lat, max_lon = (float(v) for v in bbox)
    if min_lat > max_lat:
        raise ValueError(f"bbox min_lat {min_lat} is greater than max_lat {max_lat}")
    if min_lon > max_lon:
        raise ValueError(
            f"bbox min_lon {min_lon} is greater than max_lon {max_lon} "
            "(a box straddling the antimeridian must be split into two queries)"
        )
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ValueError("bbox latitude out of range")
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ValueError("bbox longitude out of range")
    return min_lat, min_lon, max_lat, max_lon


def build_query(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    types: Iterable[str] | None = None,
    severities: Iterable[str] | None = None,
    place_icao: str | None = None,
    source: str | None = None,
    bbox: Sequence[float] | None = None,
    detector: str | None = None,
    include_invalidated: bool = False,
    limit: int = 500,
    order: str = "desc",
) -> tuple[str, list[Any]]:
    """Build a parameterised SELECT for the map.

    Every value that comes from a user or a model lands in `params`; the SQL text
    is assembled only from constants. That is the whole defence against an agent
    inventing a query.
    """
    if order.lower() not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    if not 1 <= int(limit) <= 5000:
        raise ValueError("limit must be between 1 and 5000")

    clauses: list[str] = []
    params: list[Any] = []

    if since is not None:
        clauses.append("event_time >= %s")
        params.append(_as_utc(since))
    if until is not None:
        clauses.append("event_time < %s")
        params.append(_as_utc(until))
    if since is not None and until is not None and _as_utc(since) >= _as_utc(until):
        raise ValueError("since must be earlier than until")

    if types:
        clauses.append("event_type = ANY(%s)")
        params.append([_slug(str(t)) for t in types])
    if severities:
        clauses.append("severity = ANY(%s)")
        params.append([_slug(str(s)) for s in severities])
    if place_icao:
        clauses.append("place_icao = %s")
        params.append(str(place_icao).upper())
    if source:
        if source in VIRTUAL_SOURCES:
            clauses.append(VIRTUAL_SOURCES[source])
        elif source in SOURCES:
            clauses.append("source = %s")
            params.append(source)
        else:
            raise ValueError(f"source must be one of {SOURCE_FILTERS}")

    if detector:
        if detector not in DETECTORS:
            raise ValueError(f"detector must be one of {DETECTORS}")
        clauses.append("detector = %s")
        params.append(detector)

    # Invalidated rows stay in the table for the audit trail and are never deleted, so
    # every active view has to say so explicitly. Default is the active picture.
    if not include_invalidated:
        clauses.append("invalidated_at IS NULL")

    if bbox is not None:
        min_lat, min_lon, max_lat, max_lon = _validate_bbox(bbox)
        clauses.append("lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s")
        params.extend([min_lat, max_lat, min_lon, max_lon])

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT {_COLUMNS} FROM safety_event{where} ORDER BY event_time {order.upper()} LIMIT %s"
    params.append(int(limit))
    return sql, params


def summarise_filters(**kwargs: Any) -> str:
    """One line describing a query in words — shown to the user next to results.

    A count with no description of what was counted is how a table starts lying.
    """
    parts: list[str] = []
    if kwargs.get("types"):
        parts.append("type " + "/".join(sorted(_slug(str(t)) for t in kwargs["types"])))
    if kwargs.get("severities"):
        parts.append("severity " + "/".join(sorted(_slug(str(s)) for s in kwargs["severities"])))
    if kwargs.get("place_icao"):
        parts.append(f"at {str(kwargs['place_icao']).upper()}")
    if kwargs.get("bbox"):
        min_lat, min_lon, max_lat, max_lon = (float(v) for v in kwargs["bbox"])
        parts.append(f"inside {min_lat:.2f},{min_lon:.2f} to {max_lat:.2f},{max_lon:.2f}")
    if kwargs.get("since") or kwargs.get("until"):
        since = kwargs.get("since")
        until = kwargs.get("until")
        parts.append(f"from {since:%Y-%m-%d %H:%M}Z to {until:%Y-%m-%d %H:%M}Z" if since and until
                     else f"since {since:%Y-%m-%d %H:%M}Z" if since else f"before {until:%Y-%m-%d %H:%M}Z")
    if kwargs.get("source"):
        parts.append(f"source {kwargs['source']}")
    return ", ".join(parts) if parts else "all events"


# --------------------------------------------------------------------------- #
# reads and writes
# --------------------------------------------------------------------------- #

def query_events(conn: Any, **filters: Any) -> list[dict[str, Any]]:
    sql, params = build_query(**filters)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def query_event(conn: Any, event_id: int) -> dict[str, Any] | None:
    """One event by id, including the raw payload behind it.

    The map uses this to draw the encounter OpenARIA found: the tracks it walked are
    stored in `raw`, and they are the difference between a dot and a claim you can
    check.
    """
    sql = f"SELECT {_COLUMNS}, raw FROM safety_event WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (int(event_id),))
        row = cur.fetchone()
        if row is None:
            return None
        names = [d[0] for d in cur.description]
        return dict(zip(names, row))


def count_by(conn: Any, column: str, **filters: Any) -> dict[str, int]:
    """Counts grouped by one column — the legend needs both type and severity."""
    if column not in ("event_type", "severity", "source", "place_icao"):
        raise ValueError(f"cannot group by {column!r}")
    filters.pop("limit", None)
    filters.pop("order", None)
    sql, params = build_query(limit=5000, **filters)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {column}, count(*) FROM ({sql}) AS matched GROUP BY 1 ORDER BY 2 DESC", params)
        return {row[0]: row[1] for row in cur.fetchall()}


def count_by_type(conn: Any, **filters: Any) -> dict[str, int]:
    """Counts per event type for the same filters — what the legend shows."""
    filters.pop("limit", None)
    filters.pop("order", None)
    sql, params = build_query(limit=5000, **filters)
    with conn.cursor() as cur:
        cur.execute(f"SELECT event_type, count(*) FROM ({sql}) AS matched GROUP BY 1 ORDER BY 2 DESC", params)
        return {row[0]: row[1] for row in cur.fetchall()}


def insert_events(conn: Any, events: Iterable[dict[str, Any]]) -> int:
    """Insert normalised events. Re-running a producer does not double the map.

    Returns the number of rows actually written; conflicts on
    (source, external_ref) are skipped by the unique index.
    """
    rows = [normalise_event(e) for e in events]
    if not rows:
        return 0

    sql = """
        INSERT INTO safety_event
            (event_time, event_type, severity, lat, lon, place_icao, place_name,
             altitude_ft, callsigns, icao24, registration, metrics, source,
             provenance, external_ref, raw, detector)
        VALUES (%(event_time)s, %(event_type)s, %(severity)s, %(lat)s, %(lon)s,
                %(place_icao)s, %(place_name)s, %(altitude_ft)s, %(callsigns)s,
                %(icao24)s, %(registration)s, %(metrics)s, %(source)s,
                %(provenance)s, %(external_ref)s, %(raw)s, %(detector)s)
        ON CONFLICT (source, external_ref) WHERE external_ref IS NOT NULL DO NOTHING
    """
    import json  # noqa: PLC0415

    written = 0
    with conn.cursor() as cur:
        for row in rows:
            payload = dict(row)
            payload["metrics"] = json.dumps(payload["metrics"])
            payload["raw"] = json.dumps(payload["raw"])
            cur.execute(sql, payload)
            written += cur.rowcount
    return written
