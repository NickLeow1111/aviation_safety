"""Dashboard artifact tool for occurrence-driven operational screens.

This tool complements ``chart_spec`` by assembling multiple related result sets
into one structured artifact that the frontend can render as a purpose-built
aviation operations dashboard.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def dashboard_spec(
    datasets: list[dict[str, Any]],
    title: str | None = None,
    domain: str | None = None,
    focus: str | None = None,
) -> dict[str, Any]:
    normalized = [_normalize_dataset(item) for item in datasets or []]
    dataset_map = {item["name"]: item for item in normalized if item["name"]}

    overview_rows = _pick_rows(dataset_map, "overview", "summary", "kpis")
    track_rows = _pick_rows(dataset_map, "tracks", "vectors", "aircraft")
    hotspot_rows = _pick_rows(dataset_map, "hotspots", "grid", "overlay")
    alert_rows = _pick_rows(dataset_map, "alerts", "conflicts", "intelligence")
    tactical_rows = _pick_rows(dataset_map, "tactical_audit", "audit", "risk")
    recent_rows = _pick_rows(dataset_map, "recent_records", "recent", "records", "table")
    recent_dataset = _pick_dataset(dataset_map, "recent_records", "recent", "records", "table")

    inferred_domain = (domain or _infer_domain(title, focus, normalized)).lower()
    artifact_title = title or _default_title(inferred_domain)
    metrics = _build_metrics(overview_rows, track_rows, hotspot_rows, alert_rows, recent_rows)
    tactical_card = _build_tactical_card(tactical_rows, alert_rows)
    highlights = _build_highlights(metrics, alert_rows, tactical_card, recent_rows, inferred_domain)

    return {
        "type": "ops_dashboard",
        "title": artifact_title,
        "domain": inferred_domain,
        "focus": focus or artifact_title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "hotspots": [_normalize_hotspot(row) for row in hotspot_rows[:24]],
        "tracks": [_normalize_track(row) for row in track_rows[:16]],
        "alerts": [_normalize_alert(row) for row in alert_rows[:12]],
        "tactical_audit": tactical_card,
        "recent_records": recent_rows[:12],
        "lookback_status": _pick(recent_dataset or {}, "source_window") or "rolling_12_months",
        "lookback_note": _pick(recent_dataset or {}, "note"),
        "highlights": highlights,
        "datasets": [
            {
                "name": item["name"],
                "title": item.get("title") or _title_from_name(item["name"]),
                "row_count": len(item["rows"]),
            }
            for item in normalized
        ],
    }


def _normalize_dataset(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name", "")).strip().lower().replace(" ", "_")
    rows = item.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    return {
        "name": name,
        "title": item.get("title"),
        "rows": rows,
        "source_window": item.get("source_window"),
        "note": item.get("note"),
    }


def _pick_dataset(dataset_map: dict[str, dict[str, Any]], *names: str) -> dict[str, Any] | None:
    for name in names:
        entry = dataset_map.get(name)
        if entry:
            return entry
    return None


def _pick_rows(dataset_map: dict[str, dict[str, Any]], *names: str) -> list[dict[str, Any]]:
    for name in names:
        entry = dataset_map.get(name)
        if entry:
            return entry["rows"]
    return []


def _infer_domain(title: str | None, focus: str | None, datasets: list[dict[str, Any]]) -> str:
    haystack = " ".join(
        [title or "", focus or ""]
        + [item["name"] for item in datasets]
    ).lower()
    if "bird" in haystack:
        return "bird_strike"
    if "runway" in haystack or "incursion" in haystack:
        return "runway_incursion"
    if "amo" in haystack or "audit" in haystack or "maintenance" in haystack:
        return "amo_audit"
    return "occurrence_ops"


def _default_title(domain: str) -> str:
    if domain == "bird_strike":
        return "Bird Strike Operations Dashboard"
    if domain == "runway_incursion":
        return "Runway Incursion Operations Dashboard"
    if domain == "amo_audit":
        return "AMO Quality Audit Dashboard"
    return "Occurrence Operations Dashboard"


def _build_metrics(
    overview_rows: list[dict[str, Any]],
    track_rows: list[dict[str, Any]],
    hotspot_rows: list[dict[str, Any]],
    alert_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    overview = overview_rows[0] if overview_rows else {}
    vector_count = _first_number(overview, "Active_Tracks", "Vector_Count") or len(track_rows)
    jamming_pct = _first_number(overview, "Jamming_Index_Pct", "Jamming_Pct")
    integrity_pct = _first_number(overview, "Integrity_Index_Pct", "Integrity_Pct")
    los_alerts = _first_number(overview, "Loss_Alert_Count", "LOS_Alerts")
    records_analyzed = _first_number(overview, "Records_Analyzed", "Record_Count") or len(recent_rows)

    if los_alerts is None:
        los_alerts = sum(1 for row in alert_rows if _truthy(_pick(row, "Conflict_Alert", "Status", "Alert_Status")))
    if jamming_pct is None:
        jamming_pct = _avg(track_rows, "Jamming_Index_Pct", "Jamming_Pct")
    if integrity_pct is None:
        integrity_pct = _avg(track_rows, "Integrity_Index_Pct", "Integrity_Pct")
    if not records_analyzed:
        records_analyzed = len(track_rows) + len(hotspot_rows)

    return [
        {"label": "Vectors", "value": _as_int(vector_count), "detail": "active tracks"},
        {"label": "Jamming", "value": _as_pct(jamming_pct), "detail": "signal stress"},
        {"label": "Integrity", "value": _as_pct(integrity_pct), "detail": "telemetry quality"},
        {"label": "LOS alerts", "value": _as_int(los_alerts), "detail": "active conflict cues"},
        {"label": "Records analyzed", "value": _as_int(records_analyzed), "detail": "dashboard evidence"},
    ]


def _build_tactical_card(
    tactical_rows: list[dict[str, Any]],
    alert_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    row = tactical_rows[0] if tactical_rows else (alert_rows[0] if alert_rows else None)
    if not row:
        return None

    actions = []
    for key in ("Action_1", "Action_2", "Action_3"):
      value = row.get(key)
      if value:
          actions.append(str(value))
    if not actions:
        for key in ("Recommended_Action", "Mitigation_Action", "Control_Action"):
            value = row.get(key)
            if value:
                actions.append(str(value))

    return {
        "tail_id": _pick(row, "Tail_ID", "tail_id", "Aircraft_Registration", "Track_ID") or "N/A",
        "composite_risk_score": _first_number(row, "Composite_Risk_Score", "Conflict_Risk_Score", "Risk_Score") or 0,
        "summary": _pick(row, "Intelligence_Summary", "Summary", "Risk_Statement", "Conflict_Alert") or "No tactical note available.",
        "actions": actions[:3],
    }


def _build_highlights(
    metrics: list[dict[str, str]],
    alert_rows: list[dict[str, Any]],
    tactical_card: dict[str, Any] | None,
    recent_rows: list[dict[str, Any]],
    domain: str,
) -> list[str]:
    highlights = []
    metric_map = {item["label"]: item for item in metrics}
    highlights.append(
        f"{metric_map['Vectors']['value']} monitored vectors across the current {domain.replace('_', ' ')} picture."
    )

    if alert_rows:
        primary = alert_rows[0]
        call_sign = _pick(primary, "Callsign", "Track_ID", "Tail_ID", "Aircraft_Registration") or "lead track"
        partner = _pick(primary, "Conflict_Pair", "Conflict_With")
        if partner:
            highlights.append(f"Primary alert remains between {call_sign} and {partner}.")
        else:
            highlights.append(f"Primary alert remains attached to {call_sign}.")
    elif recent_rows:
        latest = recent_rows[0]
        loc = _pick(latest, "Location", "Zone_Label", "Organisation_Name") or "the monitored area"
        highlights.append(f"Most recent activity is concentrated around {loc}.")

    if tactical_card:
        highlights.append(
            f"Tactical audit score is {int(tactical_card['composite_risk_score'])} for {tactical_card['tail_id']}."
        )

    return highlights[:3]


def _normalize_track(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "track_id": _pick(row, "Track_ID", "track_id") or "track",
        "callsign": _pick(row, "Callsign", "Flight_ID", "Track_ID") or "N/A",
        "tail_id": _pick(row, "Tail_ID", "Aircraft_Registration") or "N/A",
        "flight_level": _pick(row, "Flight_Level", "flight_level") or "FL0",
        "speed_kts": _as_int(_first_number(row, "Ground_Speed_Kts", "Speed_Kts", "Groundspeed")),
        "heading_deg": _as_int(_first_number(row, "Heading_Deg", "Heading")),
        "latitude": _first_number(row, "Latitude", "Center_Latitude", "Lat") or 0,
        "longitude": _first_number(row, "Longitude", "Center_Longitude", "Lon") or 0,
        "risk_score": _first_number(row, "Conflict_Risk_Score", "Risk_Score", "Composite_Risk_Score") or 0,
        "integrity_pct": _first_number(row, "Integrity_Index_Pct", "Integrity_Pct") or 0,
        "jamming_pct": _first_number(row, "Jamming_Index_Pct", "Jamming_Pct") or 0,
    }


def _normalize_hotspot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "zone_id": _pick(row, "Zone_ID", "Grid_Cell_ID", "Zone_Label") or "zone",
        "label": _pick(row, "Zone_Label", "Location", "Sector") or "Hotspot",
        "latitude": _first_number(row, "Center_Latitude", "Latitude", "Lat") or 0,
        "longitude": _first_number(row, "Center_Longitude", "Longitude", "Lon") or 0,
        "count": _first_number(row, "Event_Count", "Track_Count", "Conflict_Count") or 0,
        "severity": _pick(row, "Severity_Band", "Grid_Status", "Risk_Band") or "watch",
    }


def _normalize_alert(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "callsign": _pick(row, "Callsign", "Track_ID", "Tail_ID", "Aircraft_Registration") or "N/A",
        "tail_id": _pick(row, "Tail_ID", "Aircraft_Registration") or "N/A",
        "flight_level": _pick(row, "Flight_Level", "flight_level") or "FL0",
        "speed_kts": _as_int(_first_number(row, "Ground_Speed_Kts", "Speed_Kts", "Groundspeed")),
        "risk_score": _first_number(row, "Composite_Risk_Score", "Conflict_Risk_Score", "Risk_Score") or 0,
        "conflict_alert": _pick(row, "Conflict_Alert", "Risk_Statement", "Summary") or "Monitoring",
        "conflict_pair": _pick(row, "Conflict_Pair", "Conflict_With"),
    }


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _first_number(row: dict[str, Any], *keys: str) -> float | None:
    value = _pick(row, *keys)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(rows: list[dict[str, Any]], *keys: str) -> float | None:
    nums = [value for row in rows if (value := _first_number(row, *keys)) is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "closed", "normal", "none"}
    return bool(value)


def _as_int(value: float | None) -> str:
    return str(int(round(value or 0)))


def _as_pct(value: float | None) -> str:
    return f"{int(round(value or 0))}%"


def _title_from_name(name: str) -> str:
    return name.replace("_", " ").title()