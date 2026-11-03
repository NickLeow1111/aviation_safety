"""HTTP surface for the map: read events, preview an upload, import an upload.

Three endpoints, one rule between them: the preview and the import run the *same*
parser. If the preview says a row will land at a given coordinate with a given
time, that is where the import puts it. A preview produced by different code than
the write is worse than no preview at all.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from typing import Any, Sequence

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

import ingest
import openaria
import store

router = APIRouter(prefix="/api/events", tags=["events"])

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_resolver: ingest.LocationResolver | None = None


def resolver() -> ingest.LocationResolver:
    """Built once: the reference set is ~86k rows when the full file is present."""
    global _resolver
    if _resolver is None:
        _resolver = ingest.LocationResolver()
    return _resolver


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    parts = [p for p in raw.split(",") if p.strip()]
    if len(parts) != 4:
        raise HTTPException(400, "bbox must be min_lat,min_lon,max_lat,max_lon")
    try:
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    except ValueError as exc:
        raise HTTPException(400, f"bbox is not numeric: {raw}") from exc


def _parse_time(raw: str | None, name: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"{name} must be ISO 8601 (got {raw!r})") from exc


def _csv_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items or None


@router.get("")
def list_events(
    bbox: str | None = Query(None, description="min_lat,min_lon,max_lat,max_lon"),
    since: str | None = None,
    until: str | None = None,
    types: str | None = None,
    severities: str | None = None,
    place: str | None = None,
    source: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Events for the current viewport and filters, plus the counts the legend shows."""
    # A bad filter is the caller's mistake, not an outage: say so instead of blaming the store.
    if source and source not in store.SOURCE_FILTERS:
        raise HTTPException(400, f"source must be one of {store.SOURCE_FILTERS}")
    filters: dict[str, Any] = {
        "bbox": _parse_bbox(bbox),
        "since": _parse_time(since, "since"),
        "until": _parse_time(until, "until"),
        "types": _csv_list(types),
        "severities": _csv_list(severities),
        "place_icao": place,
        "source": source,
        "limit": limit,
    }
    effective = {k: v for k, v in filters.items() if v not in (None, [], "")}

    try:
        with store.connect() as conn:
            events = store.query_events(conn, **filters)
            scope = {k: v for k, v in filters.items() if k != "limit"}
            counts = store.count_by_type(conn, **scope)
            severity_counts = store.count_by(conn, "severity", **scope)
    except Exception as exc:  # noqa: BLE001 - surface a useful message, not a stack trace
        raise HTTPException(503, f"event store unavailable: {exc}") from exc

    return {
        "events": [_serialise(row) for row in events],
        "counts": counts,
        "severities": severity_counts,
        "count": len(events),
        "summary": store.summarise_filters(**effective),
        "truncated": len(events) >= int(limit),
    }


@router.get("/places")
def places(q: str = Query("", max_length=64), limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """Autocomplete over aerodromes the resolver actually knows about."""
    needle = q.strip().upper()
    known = resolver()._places  # noqa: SLF001 - same module family, read-only use
    matches = [
        {"icao": place.icao, "name": place.name, "lat": place.lat, "lon": place.lon}
        for place in known.values()
        if not needle or needle in place.icao or needle in place.name.upper()
    ]
    matches.sort(key=lambda item: (not item["icao"].startswith(needle), item["icao"]))
    return {"count": len(matches), "places": matches[:limit]}


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file is larger than {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not content.strip():
        raise HTTPException(400, "file is empty")
    return content, (file.filename or "upload.csv")


@router.post("/preview")
async def preview_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Parse and geocode a file without writing anything.

    Returns every row with its outcome, including the ones that will not import,
    so the user sees the failures before they exist anywhere.
    """
    content, filename = await _read_upload(file)
    try:
        rows = ingest.parse_upload(content, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not rows:
        raise HTTPException(400, "no data rows found — first line must be a header row")

    parsed = ingest.build_events(rows, resolver())
    ok = [item for item in parsed if not item.errors]
    failed = [item for item in parsed if item.errors]

    return {
        "filename": filename,
        "rows": [item.as_dict() for item in parsed],
        "summary": {
            "total": len(parsed),
            "ready": len(ok),
            "failed": len(failed),
            "positions": _tally(item.event["position_source"] for item in ok if item.event),
            "time_sources": _tally(item.event["time_source"] for item in ok if item.event),
            "types": _tally(item.event["event_type"] for item in ok if item.event),
        },
    }


@router.post("/import")
async def import_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Parse, geocode and write. Rows that failed the preview stay failed here."""
    content, filename = await _read_upload(file)
    try:
        rows = ingest.parse_upload(content, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    parsed = ingest.build_events(rows, resolver())
    events = ingest.to_store_events(parsed, filename)
    if not events:
        return JSONResponse(
            status_code=422,
            content={
                "imported": 0,
                "skipped": len(parsed),
                "message": "nothing could be placed honestly — fix the rows listed in the preview",
                "errors": [error for item in parsed for error in item.errors][:20],
            },
        )

    try:
        with store.connect() as conn:
            written = store.insert_events(conn, events)
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM safety_event WHERE source = 'upload'")
                uploaded_total = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"event store unavailable: {exc}") from exc

    return {
        "imported": written,
        "skipped": len(parsed) - len(events),
        "duplicates_skipped": len(events) - written,
        "uploaded_total": uploaded_total,
        "filename": filename,
    }


@router.get("/{event_id}/replay")
def event_replay(
    event_id: int,
    before_s: int = 150,
    after_s: int = 150,
    max_points: int = 900,
) -> dict[str, Any]:
    """The stored positions either side of an event, ready to step through.

    The map draws an event as a dot, which asks to be believed. This hands back the
    minutes around it — one series per aircraft — so the approach can be replayed and
    the separation watched closing and opening again.

    Nothing is interpolated here. The samples are the ones the poller stored, with the
    gaps left in; the client is responsible for saying where it is guessing between
    them.
    """
    before_s = max(10, min(int(before_s), 1800))
    after_s = max(10, min(int(after_s), 1800))

    try:
        with store.connect() as conn:
            row = store.query_event(conn, event_id)
            if row is None:
                raise HTTPException(404, f"no event with id {event_id}")

            centre = row["event_time"]
            icaos = [str(code).lower() for code in (row.get("icao24") or []) if code]
            if not icaos:
                raise HTTPException(404, "this event names no aircraft, so there is nothing to replay")

            cursor = conn.cursor()
            aircraft: list[dict[str, Any]] = []
            for code in icaos:
                cursor.execute(
                    """
                    SELECT observed_at, lat, lon, altitude_ft, ground_speed_kt,
                           track_deg, vertical_rate_fpm, on_ground, callsign
                      FROM aircraft_track
                     WHERE icao24 = %s
                       AND observed_at BETWEEN %s AND %s
                     ORDER BY observed_at
                    """,
                    (code, centre - timedelta(seconds=before_s), centre + timedelta(seconds=after_s)),
                )
                samples = cursor.fetchall()

                # A long window on a chatty aircraft can exceed the cap; thin it evenly so
                # the shape survives instead of keeping only the first page of it.
                if len(samples) > max_points:
                    stride = len(samples) / float(max_points)
                    samples = [samples[int(i * stride)] for i in range(max_points)]

                callsign = next((s[8] for s in samples if s[8]), None)
                aircraft.append(
                    {
                        "icao24": code,
                        "callsign": callsign,
                        "samples": [
                            {
                                "t": series[0].isoformat(),
                                "lat": series[1],
                                "lon": series[2],
                                "altitude_ft": series[3],
                                "ground_speed_kt": series[4],
                                "track_deg": series[5],
                                "vertical_rate_fpm": series[6],
                                "on_ground": series[7],
                            }
                            for series in samples
                        ],
                    }
                )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - say what broke, not a stack trace
        raise HTTPException(503, f"event store unavailable: {exc}") from exc

    return {
        "event_id": row["id"],
        "event_time": _serialise(row)["event_time"],
        "event_type": row["event_type"],
        "severity": row["severity"],
        "place_icao": row.get("place_icao"),
        "callsigns": row.get("callsigns"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "metrics": row.get("metrics") or {},
        "provenance": row.get("provenance"),
        "window": {"before_s": before_s, "after_s": after_s},
        "aircraft": aircraft,
    }


@router.get("/{event_id}/encounter")
def event_encounter(event_id: int) -> dict[str, Any]:
    """The tracks behind a detected encounter, ready to draw.

    Only a producer that kept its tracks gets this. OpenARIA stores the raw lines it
    walked, so the map can show the two paths that came within 0.39 NM instead of a dot
    that asks to be believed.
    """
    try:
        with store.connect() as conn:
            row = store.query_event(conn, event_id)
    except Exception as exc:  # noqa: BLE001 - say what broke, not a stack trace
        raise HTTPException(503, f"event store unavailable: {exc}") from exc

    if row is None:
        raise HTTPException(404, f"no event with id {event_id}")

    raw = row.get("raw") or {}
    payload = json.loads(raw) if isinstance(raw, str) else raw
    encounter = openaria.encounter_tracks(payload or {})
    if encounter is None:
        raise HTTPException(404, "this event has no stored tracks to draw")

    return {
        "event_id": row["id"],
        "event_time": _serialise(row)["event_time"],
        "metrics": row.get("metrics") or {},
        "provenance": row.get("provenance"),
        **encounter,
    }


def _tally(values: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe copy, including a clearly labelled airport-elevation AGL proxy.

    The event stores one recorded altitude (MSL) and a place reference. For mapped
    airport events we can subtract the reference aerodrome elevation; this is not a
    terrain-aware AGL measurement, so the UI says ``airport_agl`` rather than implying
    certified terrain clearance.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif hasattr(value, "hex") and not isinstance(value, (int, float, str)):  # Decimal-like
            out[key] = float(value)
        else:
            out[key] = value
    altitude = out.get("altitude_ft")
    place = resolver()._places.get(str(out.get("place_icao") or "").upper())  # noqa: SLF001
    if isinstance(altitude, (int, float)) and place and place.elevation_ft is not None:
        out["altitude_agl_ft"] = round(float(altitude) - place.elevation_ft)
    else:
        out["altitude_agl_ft"] = None
    out["altitude_agl_basis"] = "airport_elevation_proxy" if out["altitude_agl_ft"] is not None else None
    return out
