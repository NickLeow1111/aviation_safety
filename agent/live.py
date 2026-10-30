"""One live-traffic snapshot, shared by the map and the assistant.

Both callers go through here so the aircraft the map draws and the aircraft the
assistant counts are the same request, from the same cache, scored by the same
rules. Two paths would eventually disagree, and the assistant would be explaining
a picture the user is not looking at.
"""
from __future__ import annotations

import time
from typing import Any, Sequence

import anomaly
import detect
import opensky
import predict
import rulebook
import tracks
import tracks_db


async def snapshot(conn: Any, bbox: Sequence[float]) -> dict[str, Any]:
    payload = await opensky.live_states(conn, bbox)
    rules = rulebook.load(conn)
    payload["rules"] = rules.as_dict()
    payload["detections"] = detect.evaluate(payload["aircraft"], rules)
    payload["anomalies"] = anomaly.find(payload["aircraft"])
    # Only a genuinely fresh request may extend the tracks: a cache hit is the same
    # instant of traffic, and recording it twice would invent a stationary aircraft.
    if not payload.get("cached"):
        tracks.record(payload["aircraft"])
        if tracks_db.ensure_schema(conn):
            tracks_db.warm(conn)
            tracks_db.persist(conn, payload["aircraft"])
    payload["motion"] = tracks.motion_events()
    if not payload.get("cached") and payload["motion"]["events"]:
        tracks_db.persist_events(conn, payload["motion"]["events"])
    payload["history"] = _history(conn)
    payload["predicted"] = predict.evaluate(payload["aircraft"], rules)
    return payload


_history_cache: dict[str, Any] = {"at": 0.0, "data": {}}
HISTORY_CACHE_SECONDS = 60.0


def _history(conn: Any, days: int = 7) -> dict[str, Any]:
    """Weekly counts, cached: three queries per keystroke would be wasteful."""
    now = time.time()
    if _history_cache["data"] and now - _history_cache["at"] < HISTORY_CACHE_SECONDS:
        return _history_cache["data"]
    data = tracks_db.history_summary(conn, days)
    _history_cache.update({"at": now, "data": data})
    return data


def live_context_block(payload: dict[str, Any]) -> str:
    """Render a snapshot as the text block the assistant is allowed to quote."""
    detections = payload.get("detections") or {}
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        f"Current time: {stamp}.",  # relative questions ('this week') need a clock
        f"Live traffic snapshot: {payload.get('count', 0)} aircraft, "
        f"observed {payload.get('age_seconds', 0):.0f}s ago, OpenSky {payload.get('mode', 'anonymous')} feed "
        f"({payload.get('credits_used')}/{payload.get('credits_budget')} credits used today).",
        f"Active rules: {detections.get('rules', {})}.",
        detections.get("summary", ""),
    ]

    conflicts = detections.get("conflicts") or []
    if conflicts:
        lines.append("Aircraft currently inside the separation minimum:")
        for item in conflicts[:10]:
            vertical = item.get("vertical_ft")
            lines.append(
                f"  {item['a']} / {item['b']} — {item['separation_nm']} NM apart"
                + (f", {vertical} ft vertical" if vertical is not None else ", vertical unknown")
            )
    else:
        lines.append("No pair of aircraft is currently inside the separation minimum.")

    overspeed = detections.get("overspeed") or []
    if overspeed:
        lines.append("Aircraft above the speed limit:")
        for item in overspeed[:10]:
            lines.append(
                f"  {item['callsign']} — {item['ground_speed_kt']} kt"
                + (f" at {item['altitude_ft']} ft" if item.get("altitude_ft") is not None else "")
            )
    else:
        lines.append("No aircraft is above the speed limit.")

    unusual = payload.get("anomalies") or {}
    if unusual.get("flags"):
        lines.append("Unusual flight behaviour in this snapshot:")
        for item in unusual["flags"][:12]:
            lines.append(f"  {item['callsign']} — {item['detail']} ({item['kind']})")
        lines.append("  " + unusual.get("disclaimer", ""))
    elif unusual:
        lines.append("No unusual flight behaviour detected in this snapshot.")

    predicted = payload.get("predicted") or {}
    if predicted.get("conflicts"):
        lines.append(
            f"Predicted separation losses within {predicted.get('horizon_s')} seconds "
            "(dead reckoning, straight-line, projection only):"
        )
        for item in predicted["conflicts"][:10]:
            vertical = item.get("vertical_ft")
            lines.append(
                f"  {item['a']} / {item['b']} — now {item['current_nm']} NM, "
                f"predicted {item['predicted_nm']} NM in {item['time_to_cpa_s']}s"
                + (f", {vertical} ft vertical then" if vertical is not None else "")
            )
    elif predicted:
        lines.append(
            f"No pair is predicted to lose separation within {predicted.get('horizon_s')} seconds."
        )

    motion = payload.get("motion") or {}
    if motion.get("events"):
        lines.append("Manoeuvres seen in the recent tracks:")
        for item in motion["events"][:10]:
            lines.append(f"  {item['callsign']} — {item['detail']} ({item['kind']})")
        lines.append("  " + motion.get("disclaimer", ""))
    elif motion:
        lines.append("No unusual manoeuvre in the tracks seen so far.")

    history = payload.get("history") or {}
    if history:
        lines.append(
            f"Detector history (manoeuvres stored in the track_event table): "
            f"{tracks_db.render_summary(history)}"
        )

    if detections.get("disclaimer"):
        lines.append(
            "Note for the answer: pairs currently inside the minimum come from a snapshot; "
            "pairs in the 'predicted' list are projections. Keep the two separate and say "
            "so when you report them."
        )
    return "\n".join(line for line in lines if line)
