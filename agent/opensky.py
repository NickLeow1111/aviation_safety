"""Live aircraft from OpenSky Network, with the meter running.

OpenSky bills `/states/all` by **bounding-box area**, not by call count: a box up
to 25 square degrees costs 1 credit, and the daily allowance is 400 credits
anonymous or 4000 authenticated. So the design constraints are:

  * one server-side poller with a cache — never one request per browser tab;
  * a cache TTL, so opening the map twice in five seconds costs one credit;
  * a hard daily budget recorded in Postgres, not in a process-local counter that
    resets when uvicorn reloads;
  * anonymous mode works, because a missing credential should degrade the budget,
    not the feature.

Credentials (optional) come from the environment as OPENSKY_CLIENT_ID and
OPENSKY_CLIENT_SECRET — OAuth2 client credentials, not a single API key. They are
never logged and never echoed back through the API.
"""
from __future__ import annotations

import asyncio
import envfile
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
STATES_URL = "https://opensky-network.org/api/states/all"

# Credits per day, by credential mode, from OpenSky's published limits.
DAILY_CREDITS = {"anonymous": 400, "authenticated": 4000}
# Leave headroom: the account may be shared with other things (or with a human
# poking at the API), and hitting the ceiling looks like an outage.
BUDGET_FRACTION = 0.85

CACHE_TTL_SECONDS = 10.0
# A bbox larger than this costs more than one credit and is almost never what a
# viewer means; refuse it and tell them to zoom in.
MAX_BBOX_SQUARE_DEGREES = 25.0

_STATE_FIELDS = (
    "icao24",
    "callsign",
    "origin_country",
    "time_position",
    "last_contact",
    "lon",
    "lat",
    "baro_altitude",
    "on_ground",
    "velocity",
    "true_track",
    "vertical_rate",
    "sensors",
    "geo_altitude",
    "squawk",
    "spi",
    "position_source",
)


class OpenSkyError(RuntimeError):
    """Anything the caller should surface to a user rather than a traceback."""


@dataclass
class Usage:
    day: date
    mode: str
    requests: int
    credits: int

    @property
    def budget(self) -> int:
        return int(DAILY_CREDITS.get(self.mode, DAILY_CREDITS["anonymous"]) * BUDGET_FRACTION)

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.credits)


def credentials() -> tuple[str | None, str | None]:
    # Load lazily so a CLI call and the server both see agent/.env.local, and so a
    # credentials file added after boot is picked up without touching app.py.
    if not (os.environ.get("OPENSKY_CLIENT_ID") and os.environ.get("OPENSKY_CLIENT_SECRET")):
        try:
            # force: a credential file written after boot must be picked up without
            # a restart, otherwise the loader latches "already loaded, nothing there".
            envfile.load(force=True)
        except Exception:  # noqa: BLE001 - never break the poller over a bad file
            pass
    return os.environ.get("OPENSKY_CLIENT_ID"), os.environ.get("OPENSKY_CLIENT_SECRET")


def credential_mode() -> str:
    client_id, secret = credentials()
    return "authenticated" if client_id and secret else "anonymous"


def bbox_area(bbox: Sequence[float]) -> float:
    """Square degrees covered — the unit OpenSky charges in."""
    min_lat, min_lon, max_lat, max_lon = (float(v) for v in bbox)
    return abs(max_lat - min_lat) * abs(max_lon - min_lon)


def credits_for(bbox: Sequence[float]) -> int:
    """1 credit up to 25 sq deg, then linearly by area, per OpenSky's schedule."""
    area = bbox_area(bbox)
    if area <= MAX_BBOX_SQUARE_DEGREES:
        return 1
    return 1 + int(area // MAX_BBOX_SQUARE_DEGREES)


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #

_token_lock = asyncio.Lock()
_token: dict[str, Any] = {"value": None, "expires_at": 0.0}


async def access_token() -> str | None:
    """OAuth2 client-credentials token, cached until shortly before it expires."""
    client_id, secret = credentials()
    if not (client_id and secret):
        return None

    async with _token_lock:
        if _token["value"] and time.time() < _token["expires_at"] - 60:
            return _token["value"]

        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
        ).encode()

        def _fetch() -> dict[str, Any]:
            request = urllib.request.Request(
                TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                return json.loads(response.read())

        try:
            payload = await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001 - degrade to anonymous rather than fail
            # Deliberately does not include the exception text: a failed token call
            # can echo the request body, and the body contains the secret.
            raise OpenSkyError(f"OpenSky token request failed ({type(exc).__name__}); falling back to anonymous") from None

        _token["value"] = payload.get("access_token")
        _token["expires_at"] = time.time() + float(payload.get("expires_in", 1800))
        return _token["value"]


# --------------------------------------------------------------------------- #
# usage accounting
# --------------------------------------------------------------------------- #

def _ensure_schema(conn: Any) -> None:
    from pathlib import Path

    schema = Path(__file__).resolve().parents[1] / "db" / "002_live_and_documents.sql"
    if not getattr(_ensure_schema, "_done", False):
        with conn.cursor() as cur:
            cur.execute(schema.read_text(encoding="utf-8"))
        _ensure_schema._done = True  # type: ignore[attr-defined]


def usage_today(conn: Any) -> Usage:
    mode = credential_mode()
    _ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT requests, credits FROM opensky_usage WHERE day = %s AND mode = %s",
            (datetime.now(timezone.utc).date(), mode),
        )
        row = cur.fetchone()
    requests, credits = (row if row else (0, 0))
    return Usage(datetime.now(timezone.utc).date(), mode, requests, credits)


def record_spend(conn: Any, credits: int) -> Usage:
    mode = credential_mode()
    today = datetime.now(timezone.utc).date()
    _ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO opensky_usage (day, mode, requests, credits, last_call)
            VALUES (%s, %s, 1, %s, now())
            ON CONFLICT (day, mode) DO UPDATE
               SET requests = opensky_usage.requests + 1,
                   credits  = opensky_usage.credits + EXCLUDED.credits,
                   last_call = now()
            RETURNING requests, credits
            """,
            (today, mode, credits),
        )
        requests, total = cur.fetchone()
    return Usage(today, mode, requests, total)


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

_cache: dict[str, dict[str, Any]] = {}
_fetch_lock = asyncio.Lock()


def _cache_key(bbox: Sequence[float]) -> str:
    # Rounded so two viewers looking at roughly the same area share one request.
    return ",".join(f"{float(v):.2f}" for v in bbox)


def _normalise_state(raw: list[Any]) -> dict[str, Any]:
    record = dict(zip(_STATE_FIELDS, raw))
    callsign = (record.get("callsign") or "").strip()
    return {
        "icao24": record.get("icao24"),
        "callsign": callsign or None,
        "country": record.get("origin_country"),
        "lat": record.get("lat"),
        "lon": record.get("lon"),
        "altitude_ft": (
            round(float(record["baro_altitude"]) * 3.28084) if record.get("baro_altitude") is not None else None
        ),
        "on_ground": bool(record.get("on_ground")),
        "ground_speed_kt": (
            round(float(record["velocity"]) * 1.94384) if record.get("velocity") is not None else None
        ),
        "track_deg": record.get("true_track"),
        "vertical_rate_fpm": (
            round(float(record["vertical_rate"]) * 196.85) if record.get("vertical_rate") is not None else None
        ),
        "squawk": record.get("squawk"),
        "last_contact": record.get("last_contact"),
    }


def _request_states(bbox: Sequence[float], token: str | None) -> dict[str, Any]:
    min_lat, min_lon, max_lat, max_lon = (float(v) for v in bbox)
    query = urllib.parse.urlencode(
        {"lamin": min_lat, "lomin": min_lon, "lamax": max_lat, "lomax": max_lon, "extended": 0}
    )
    headers = {"User-Agent": "safetyintelligence-poc/0.2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{STATES_URL}?{query}", headers=headers)
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read())


async def live_states(conn: Any, bbox: Sequence[float]) -> dict[str, Any]:
    """Aircraft inside `bbox`, served from cache when it is fresh.

    Returns the aircraft plus the accounting the UI must show: which credential
    mode answered, how much of the daily budget is left, and how old the data is.
    A map that shows live traffic without showing its age is telling a lie about
    the present tense.
    """
    area = bbox_area(bbox)
    if area > MAX_BBOX_SQUARE_DEGREES:
        raise OpenSkyError(
            f"view is too large for the live feed: {area:.1f} sq deg "
            f"(OpenSky bills by area; keep it under {MAX_BBOX_SQUARE_DEGREES:.0f})"
        )

    key = _cache_key(bbox)
    cached = _cache.get(key)
    now = time.time()
    if cached and now - cached["fetched_at"] < CACHE_TTL_SECONDS:
        payload = dict(cached["payload"])
        payload["age_seconds"] = round(now - cached["fetched_at"], 1)
        payload["cached"] = True
        return payload

    async with _fetch_lock:
        cached = _cache.get(key)
        if cached and time.time() - cached["fetched_at"] < CACHE_TTL_SECONDS:
            payload = dict(cached["payload"])
            payload["age_seconds"] = round(time.time() - cached["fetched_at"], 1)
            payload["cached"] = True
            return payload

        usage = usage_today(conn)
        cost = credits_for(bbox)
        if usage.remaining < cost:
            raise OpenSkyError(
                f"daily OpenSky budget reached ({usage.credits}/{usage.budget} credits, "
                f"{usage.mode} mode). Reset at 00:00 UTC."
            )

        token = None
        mode = credential_mode()
        if mode == "authenticated":
            try:
                token = await access_token()
            except OpenSkyError:
                token = None

        try:
            payload = await asyncio.to_thread(_request_states, bbox, token)
        except Exception as exc:  # noqa: BLE001
            raise OpenSkyError(f"OpenSky request failed: {type(exc).__name__}") from None

        spend = record_spend(conn, cost)
        states = payload.get("states") or []
        aircraft = [
            _normalise_state(state)
            for state in states
            if state and len(state) >= len(_STATE_FIELDS) - 1
        ]
        result = {
            "aircraft": aircraft,
            "count": len(aircraft),
            "observed_at": payload.get("time"),
            "mode": spend.mode,
            "credits_used": spend.credits,
            "credits_budget": spend.budget,
            "credits_remaining": spend.remaining,
            "credits_spent_on_call": cost,
            "age_seconds": 0.0,
            "cached": False,
        }
        _cache[key] = {"payload": result, "fetched_at": time.time()}
        return result


def cache_size() -> int:
    return len(_cache)
