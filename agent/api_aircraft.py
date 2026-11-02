"""One flight's recent profile.

The aircraft card on the map shows an instant: altitude now, speed now. The question behind a
callsign is usually "what is it doing?" — climbing out, holding, descending into Changi — and
that lives in the fixes we already stored. This endpoint reads them; it never asks OpenSky.

Everything here is observed. If the aircraft has three fixes we say three, and the gaps are
visible in the timestamps rather than smoothed away.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import store

router = APIRouter(prefix="/api/aircraft", tags=["aircraft"])

MAX_SAMPLES = 240


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@router.get("/lookup")
def lookup(callsign: str, minutes: int = Query(15, ge=1, le=180)) -> dict[str, Any]:
    """The last place this callsign was seen, wherever that was.

    The map's live list only holds aircraft inside the current viewport. A flight named on an
    event may be a hundred miles away and still flying, so asking for it by callsign should
    find it from the stored fixes rather than reporting it absent.
    """
    wanted = callsign.strip().upper()
    if not wanted:
        raise HTTPException(status_code=400, detail="a callsign is required")

    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT icao24, callsign, lat, lon, altitude_ft, ground_speed_kt, track_deg,
                   vertical_rate_fpm, on_ground, observed_at
              FROM aircraft_track
             WHERE upper(trim(callsign)) = %s
               AND observed_at > now() - make_interval(mins => %s)
             ORDER BY observed_at DESC
             LIMIT 1
            """,
            (wanted, minutes),
        )
        row = cur.fetchone()

    if row is None:
        return {"found": False, "callsign": wanted, "minutes": minutes, "fix": None,
                "note": f"No fix for {wanted} in the last {minutes} minutes."}

    age_s = max((datetime.now(timezone.utc) - row[9].replace(tzinfo=timezone.utc)).total_seconds(), 0.0) if row[9] else None
    return {
        "found": True,
        "callsign": row[1] or wanted,
        "minutes": minutes,
        "fix": {
            "icao24": row[0],
            "callsign": row[1],
            "lat": row[2],
            "lon": row[3],
            "altitude_ft": row[4],
            "ground_speed_kt": row[5],
            "track_deg": row[6],
            "vertical_rate_fpm": row[7],
            "on_ground": row[8],
            "observed_at": _iso(row[9]),
            "age_s": round(age_s, 1) if age_s is not None else None,
        },
        "note": None,
    }


@router.get("/{icao24}/profile")
def profile(icao24: str, minutes: int = Query(20, ge=2, le=180)) -> dict[str, Any]:
    """Recent fixes for one aircraft, oldest first, plus what they add up to."""
    code = icao24.strip().lower()
    if not code:
        raise HTTPException(status_code=400, detail="an icao24 is required")

    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, lat, lon, altitude_ft, ground_speed_kt, track_deg,
                   vertical_rate_fpm, on_ground, callsign, aircraft_type
              FROM aircraft_track
             WHERE icao24 = %s
               AND observed_at > now() - make_interval(mins => %s)
             ORDER BY observed_at DESC
             LIMIT %s
            """,
            (code, minutes, MAX_SAMPLES),
        )
        rows = cur.fetchall()

    if not rows:
        return {
            "icao24": code,
            "callsign": None,
            "minutes": minutes,
            "samples": [],
            "summary": None,
            "note": f"No fixes for {code} in the last {minutes} minutes — it may not be receiving here.",
        }

    rows = list(reversed(rows))  # oldest first, so the line reads left to right
    samples = [
        {
            "t": _iso(row[0]),
            "lat": row[1],
            "lon": row[2],
            "altitude_ft": row[3],
            "ground_speed_kt": row[4],
            "track_deg": row[5],
            "vertical_rate_fpm": row[6],
            "on_ground": row[7],
        }
        for row in rows
    ]

    altitudes = [row[3] for row in rows if row[3] is not None]
    speeds = [row[4] for row in rows if row[4] is not None]
    callsign = next((row[8] for row in reversed(rows) if row[8]), None)

    # The span is the honest part: the same callsign can be missing for minutes at a time.
    first, last = rows[0][0], rows[-1][0]
    span_s = (last - first).total_seconds() if first and last else 0.0
    expected = max(int(span_s // 30) + 1, 1)

    summary = {
        "fixes": len(rows),
        "span_s": round(span_s, 1),
        # A coverage figure, not a quality score: how many 30-second slots carry a fix.
        "coverage": round(min(len(rows) / expected, 1.0), 2),
        "altitude_min_ft": min(altitudes) if altitudes else None,
        "altitude_max_ft": max(altitudes) if altitudes else None,
        "ground_speed_kt_min": min(speeds) if speeds else None,
        "ground_speed_kt_max": max(speeds) if speeds else None,
        "aircraft_type": next((row[9] for row in reversed(rows) if row[9]), None),
        "on_ground": bool(rows[-1][7]),
    }

    return {
        "icao24": code,
        "callsign": callsign,
        "minutes": minutes,
        "samples": samples,
        "summary": summary,
        "note": None,
    }
