"""The community ADS-B feed: fresher than OpenSky, thinner, and free.

OpenSky batches state vectors — in testing, positions were a median of 11 seconds old
inside a 25 NM box. The community network (adsb.lol) answered with fixes a median of
0.46 seconds old for the same sky, but only saw 8 of the 23 aircraft OpenSky had. So
neither source is a replacement for the other: this module is the dense half, and the
poller merges the two by aircraft id.

Two rules learned the hard way while testing the endpoint:

* **It throttles by IP without advertising a limit.** Two bursts of a dozen requests
  earned a 429; a datacenter address is a crowded neighbourhood. `_gate()` spaces
  requests and honours a backoff after a refusal.
* **`alt_baro` can be the string "ground".** A feed that reports a parked aircraft as
  an altitude is a parser that will eventually divide by it.

Licence note: adsb.lol publishes its data under ODbL (attribution + share-alike).
Anything we show publicly from this feed has to carry that attribution.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

POINT_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}"
USER_AGENT = "safety-intel/1.0 (Singapore aviation safety map; contact: leowwenliang@gmail.com)"
MIN_REQUEST_GAP_SECONDS = 1.2      # the endpoint's documented ceiling is ~1 req/s
BACKOFF_AFTER_429_SECONDS = 120.0

_next_allowed = 0.0


class CommunityFeedError(RuntimeError):
    """The community feed did not answer with something we can use."""


def _gate(now: float | None = None) -> float:
    """Wait long enough to be polite. Returns how long we slept, in seconds."""
    global _next_allowed
    moment = time.time() if now is None else now
    wait = max(0.0, _next_allowed - moment)
    if wait:
        time.sleep(wait)
    _next_allowed = max(moment, _next_allowed) + MIN_REQUEST_GAP_SECONDS
    return wait


def _backoff(seconds: float = BACKOFF_AFTER_429_SECONDS) -> None:
    global _next_allowed
    _next_allowed = max(_next_allowed, time.time() + seconds)


def normalise(payload: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    """Turn one adsb.lol response into the same shape OpenSky aircraft arrive in.

    A fix missing a position is dropped: a track with a hole is worse than a track
    that starts later.
    """
    moment = time.time() if now is None else now
    aircraft: list[dict[str, Any]] = []
    for item in payload.get("ac") or []:
        icao24 = str(item.get("hex") or "").strip().lower()
        lat, lon = item.get("lat"), item.get("lon")
        if not icao24 or lat is None or lon is None:
            continue

        raw_alt = item.get("alt_baro")
        on_ground = raw_alt == "ground" or bool(item.get("ground"))
        if isinstance(raw_alt, (int, float)):
            altitude: float | None = float(raw_alt)
        else:
            altitude = 0.0 if on_ground else None

        seen = item.get("seen_pos")
        fixed_at = moment - float(seen) if isinstance(seen, (int, float)) else moment
        callsign = (item.get("flight") or "").strip()

        aircraft.append(
            {
                "icao24": icao24,
                "callsign": callsign or None,
                "lat": float(lat),
                "lon": float(lon),
                "altitude_ft": altitude,
                "ground_speed_kt": item.get("gs"),
                "track_deg": item.get("track"),
                "vertical_rate_fpm": item.get("baro_rate"),
                "on_ground": on_ground,
                "observed_at": fixed_at,
                "source": "adsb.lol",
                # adsb.lol is the only feed of the two that says what the aircraft is.
                "aircraft_type": (str(item.get("t") or "").strip().upper() or None),
            }
        )
    return aircraft


def fetch(lat: float, lon: float, radius_nm: int = 100, timeout: float = 30.0) -> list[dict[str, Any]]:
    """One read of the feed around a point. Raises CommunityFeedError, never returns junk."""
    _gate()
    url = POINT_URL.format(lat=lat, lon=lon, radius=radius_nm)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — fixed https host
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _backoff()
            raise CommunityFeedError(f"adsb.lol rate-limited this host (HTTP 429); backing off {BACKOFF_AFTER_429_SECONDS:.0f}s") from None
        raise CommunityFeedError(f"adsb.lol returned HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 — any transport failure is the same to the caller
        raise CommunityFeedError(f"adsb.lol unreachable: {type(exc).__name__}") from None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise CommunityFeedError("adsb.lol returned something that is not JSON") from None
    if not isinstance(payload, dict):
        raise CommunityFeedError("adsb.lol returned an unexpected shape")
    return normalise(payload)


def fetch_many(points: Iterable[tuple[float, float]], radius_nm: int = 100) -> list[dict[str, Any]]:
    """Read several centres and concatenate. Used when one circle cannot cover the box."""
    fixes: list[dict[str, Any]] = []
    for lat, lon in points:
        fixes.extend(fetch(lat, lon, radius_nm=radius_nm))
    return fixes
