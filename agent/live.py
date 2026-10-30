"""One live-traffic snapshot, shared by the map and the assistant.

Both callers go through here so the aircraft the map draws and the aircraft the
assistant counts are the same request, from the same cache, scored by the same
rules. Two paths would eventually disagree, and the assistant would be explaining
a picture the user is not looking at.
"""
from __future__ import annotations

from typing import Any, Sequence

import anomaly
import detect
import opensky
import rulebook


async def snapshot(conn: Any, bbox: Sequence[float]) -> dict[str, Any]:
    payload = await opensky.live_states(conn, bbox)
    rules = rulebook.load(conn)
    payload["rules"] = rules.as_dict()
    payload["detections"] = detect.evaluate(payload["aircraft"], rules)
    payload["anomalies"] = anomaly.find(payload["aircraft"])
    return payload


def live_context_block(payload: dict[str, Any]) -> str:
    """Render a snapshot as the text block the assistant is allowed to quote."""
    detections = payload.get("detections") or {}
    lines = [
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

    if detections.get("disclaimer"):
        lines.append(
            "Note for the answer: this is a snapshot of position reports, not a conflict "
            "prediction — say so if you report a conflict."
        )
    return "\n".join(line for line in lines if line)
