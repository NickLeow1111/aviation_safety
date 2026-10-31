"""Poll the sky on a timer, so the tracks are worth analysing.

Until now capture was a side effect of watching: every stored position came from a
browser opening the map, which is why the average aircraft in the store has 19 fixes
spread over 43 minutes — and why OpenARIA could only analyse one pair. This module
polls whether or not anyone is looking.

Two sources, merged by aircraft id:

* **OpenSky** — broad coverage, 10-second state vectors, 3,400 credits a day. One poll
  of a 20-square-degree box costs one credit, so the budget caps a continuous cadence
  at roughly one fix per 25 seconds across a whole day.
* **adsb.lol** — free and keyless, sub-second fixes, but it only sees about a third of
  the traffic in this airspace and throttles by IP without saying so.

The cadence is not a constant. `plan_interval` recomputes it every loop from the
credits actually left and the hours actually remaining, so a busy morning stretches
the interval instead of spending the whole day's allowance before lunch and leaving
the evening blind.

What this does not do: it does not create the event table, and it never fetches the
same instant twice — a cache hit is the same traffic, and storing it again would
invent a stationary aircraft.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable, Sequence

import community_adsb
import opensky
import promote
import store
import tracks
import tracks_db

log = logging.getLogger("safety-intel-poller")

# A 20 square-degree box is billed as one credit — the same as a tiny one — so we take
# Singapore and the approaches on both sides rather than the map's viewport.
DEFAULT_BBOX = (0.5, 102.5, 4.5, 107.5)

MIN_INTERVAL_SECONDS = 15.0
MAX_INTERVAL_SECONDS = 120.0
# Two sources describe the same aircraft seconds apart; storing both would double the
# track and halve its apparent speed.
MIN_STORE_GAP_SECONDS = 5.0
COMMUNITY_EXTRA_FRESH_SECONDS = 2.0
DAILY_RESET = dtime(0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# planning (pure)
# --------------------------------------------------------------------------- #

def seconds_left_utc(now: datetime | None = None) -> float:
    """Seconds until OpenSky's day rolls over (their clock, not ours)."""
    moment = now or datetime.now(timezone.utc)
    tomorrow = datetime.combine(moment.date(), DAILY_RESET) + timedelta(days=1)
    return max(0.0, (tomorrow - moment).total_seconds())


def plan_interval(
    credits_remaining: float,
    credits_per_poll: float = 1.0,
    seconds_remaining: float | None = None,
    floor: float = MIN_INTERVAL_SECONDS,
    ceiling: float = MAX_INTERVAL_SECONDS,
) -> float:
    """How long to wait before the next OpenSky poll.

    Spreads what is left evenly across the rest of the day rather than burning it at a
    fixed rate: when the budget is intact the cadence sits at the floor, and as the day
    runs down it stretches towards the ceiling rather than going silent.
    """
    if seconds_remaining is None:
        seconds_remaining = seconds_left_utc()
    if credits_remaining <= 0:
        return ceiling
    affordable = max(1.0, credits_remaining / max(credits_per_poll, 0.001))
    return max(floor, min(ceiling, seconds_remaining / affordable))


def merge_fixes(
    primary: Iterable[dict[str, Any]],
    community: Iterable[dict[str, Any]],
    extra_fresh_seconds: float = COMMUNITY_EXTRA_FRESH_SECONDS,
) -> list[dict[str, Any]]:
    """One fix per aircraft: OpenSky's fields, but the community antenna's position when
    it is meaningfully newer."""
    merged: dict[str, dict[str, Any]] = {}
    for fix in primary:
        key = str(fix.get("icao24") or "").lower()
        if key:
            merged[key] = dict(fix)

    for fix in community:
        key = str(fix.get("icao24") or "").lower()
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(fix)
            continue
        primary_age = float(_age_seconds(existing))
        community_age = float(_age_seconds(fix))
        if primary_age - community_age >= extra_fresh_seconds:
            enriched = dict(existing)
            enriched.update(
                {
                    "lat": fix["lat"],
                    "lon": fix["lon"],
                    "observed_at": fix.get("observed_at"),
                    "community_source": fix.get("source"),
                }
            )
            if fix.get("altitude_ft") is not None:
                enriched["altitude_ft"] = fix["altitude_ft"]
            merged[key] = enriched
    return list(merged.values())


def _age_seconds(fix: dict[str, Any]) -> float:
    observed = fix.get("observed_at")
    if isinstance(observed, (int, float)):
        return max(0.0, time.time() - float(observed))
    return 0.0


def should_store(icao24: str, moment: float, last_written: dict[str, float], min_gap: float = MIN_STORE_GAP_SECONDS) -> bool:
    """Has enough time passed to record this aircraft again?"""
    previous = last_written.get(icao24)
    return previous is None or moment - previous >= min_gap


def circle_for_bbox(bbox: Sequence[float]) -> tuple[float, float, int]:
    """The (lat, lon, radius_nm) circle that covers a box, capped at the feed's 250 NM."""
    min_lat, min_lon, max_lat, max_lon = (float(v) for v in bbox)
    lat = (min_lat + max_lat) / 2
    lon = (min_lon + max_lon) / 2
    dlat = (max_lat - min_lat) / 2 * 60
    dlon = (max_lon - min_lon) / 2 * 60 * math.cos(math.radians(lat))
    radius = int(min(250, max(25, math.hypot(dlat, dlon))))
    return lat, lon, radius


# --------------------------------------------------------------------------- #
# one poll
# --------------------------------------------------------------------------- #

async def poll_once(
    conn: Any,
    *,
    bbox: Sequence[float] = DEFAULT_BBOX,
    use_community: bool = True,
    state: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch, merge, store. Returns a summary the loop uses to plan the next interval."""
    state = state if state is not None else {"last_written": {}}
    moment = time.time()
    result: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    try:
        payload = await opensky.live_states(conn, bbox)
    except opensky.OpenSkyError as exc:
        result.update({"error": f"opensky: {exc}", "stored": 0})
        log.warning("OpenSky refused the poll: %s", exc)
        return result

    aircraft = list(payload.get("aircraft") or [])
    result.update(
        {
            "opensky_aircraft": len(aircraft),
            "cached": bool(payload.get("cached")),
            "credits_remaining": payload.get("credits_remaining"),
            "credits_budget": payload.get("credits_budget"),
            "credits_spent": payload.get("credits_spent_on_call", 0),
        }
    )

    if payload.get("cached"):
        # Same instant of traffic as the last poll; recording it again would draw a
        # stationary aircraft.
        result.update({"stored": 0, "note": "cache hit"})
        return result

    if use_community:
        lat, lon, radius = circle_for_bbox(bbox)
        try:
            community = await asyncio.to_thread(community_adsb.fetch, lat, lon, radius)
            result["community_aircraft"] = len(community)
            aircraft = merge_fixes(aircraft, community)
        except community_adsb.CommunityFeedError as exc:
            result["community_error"] = str(exc)
            log.warning("community feed unavailable: %s", exc)

    fresh = [
        fix
        for fix in aircraft
        if fix.get("icao24") and should_store(str(fix["icao24"]).lower(), moment, state["last_written"])
    ]
    result["merged_aircraft"] = len(aircraft)
    result["fresh"] = len(fresh)

    if not dry_run and fresh:
        tracks.record(fresh)
        stored = tracks_db.persist(conn, fresh)
        for fix in fresh:
            state["last_written"][str(fix["icao24"]).lower()] = moment
        result["stored"] = stored

        motion = tracks.motion_events()
        events = motion.get("events") or []
        if events:
            tracks_db.persist_events(conn, events)
            try:
                promoted = promote.from_motion(events, moment, promote.load_aerodromes())
                result["events_promoted"] = store.insert_events(conn, promoted) if promoted else 0
            except Exception as exc:  # noqa: BLE001 — the record is a bonus, the tracks are the product
                conn.rollback()
                log.warning("could not promote manoeuvres: %s", exc)
    else:
        result["stored"] = 0

    return result


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #

async def run_forever(
    *,
    bbox: Sequence[float],
    use_community: bool,
    min_interval: float,
    max_interval: float,
    limit_seconds: float | None = None,
) -> None:
    state: dict[str, Any] = {"last_written": {}}
    started = time.time()
    _stopping = {"stop": False}

    def _stop(signum: int, _frame: Any) -> None:
        log.info("signal %s received; finishing this poll then stopping", signum)
        _stopping["stop"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except ValueError:  # pragma: no cover — not on the main thread
            pass

    while not _stopping["stop"]:
        with store.connect() as conn:
            result = await poll_once(conn, bbox=bbox, use_community=use_community, state=state)
        log.info(
            "poll: opensky=%s community=%s merged=%s fresh=%s stored=%s credits_left=%s%s",
            result.get("opensky_aircraft"),
            result.get("community_aircraft", "-"),
            result.get("merged_aircraft"),
            result.get("fresh"),
            result.get("stored"),
            result.get("credits_remaining", "?"),
            f" error={result['error']}" if result.get("error") else "",
        )

        interval = plan_interval(
            credits_remaining=float(result.get("credits_remaining") or 0),
            credits_per_poll=float(result.get("credits_spent") or 1),
            floor=min_interval,
            ceiling=max_interval,
        )
        if limit_seconds is not None and time.time() - started + interval > limit_seconds:
            log.info("time limit reached; stopping")
            break
        await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll live traffic on a budget-aware timer and store the tracks.")
    parser.add_argument("--bbox", default=",".join(str(v) for v in DEFAULT_BBOX), help="min_lat,min_lon,max_lat,max_lon")
    parser.add_argument("--no-community", action="store_true", help="OpenSky only")
    parser.add_argument("--min-interval", type=float, default=MIN_INTERVAL_SECONDS)
    parser.add_argument("--max-interval", type=float, default=MAX_INTERVAL_SECONDS)
    parser.add_argument("--duration", type=float, help="stop after N seconds (for a smoke test)")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, store nothing")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    bbox = tuple(float(v) for v in args.bbox.split(","))
    if len(bbox) != 4:
        parser.error("--bbox needs four comma-separated numbers")

    if args.once or args.dry_run:
        async def _single() -> None:
            state: dict[str, Any] = {"last_written": {}}
            with store.connect() as conn:
                result = await poll_once(
                    conn, bbox=bbox, use_community=not args.no_community, state=state, dry_run=args.dry_run
                )
            for key, value in result.items():
                print(f"{key}: {value}")

        asyncio.run(_single())
        return 0

    asyncio.run(
        run_forever(
            bbox=bbox,
            use_community=not args.no_community,
            min_interval=args.min_interval,
            max_interval=args.max_interval,
            limit_seconds=args.duration,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
