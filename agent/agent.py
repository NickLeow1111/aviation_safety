"""Safety Intelligence Bot — Foundry v2 Responses API client.

Talks to a Microsoft Foundry **PromptAgent** (created via
`scripts/create_foundry_agent.py`) using the OpenAI Responses API surfaced
through `AIProjectClient.get_openai_client(agent_name=...)`. No legacy
Assistants / Persistent Agents API.

Env vars required at runtime:
    AZURE_AI_PROJECT_ENDPOINT   Foundry project endpoint (https://...)
    FOUNDRY_AGENT_NAME          v2 agent name (e.g. "safety-intelligence-bot")
                                Optionally pinned to a version: "name:1"

Auth: Microsoft Entra ID only (DefaultAzureCredential / MI).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from tools import chart_spec, dashboard_spec, doc_search, run_nl2sql

log = logging.getLogger(__name__)

_credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
_project: AIProjectClient | None = None
_openai = None
# session_id -> previous_response_id (stitches turns into one Foundry conversation)
_sessions: dict[str, str] = {}


def _project_client() -> AIProjectClient:
    global _project
    if _project is None:
        endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
        _project = AIProjectClient(
            endpoint=endpoint,
            credential=_credential,
            allow_preview=True,  # required to point OpenAI client at agent endpoint
        )
    return _project


def _agent_name() -> str:
    name = os.environ.get("FOUNDRY_AGENT_NAME")
    if not name:
        raise RuntimeError(
            "FOUNDRY_AGENT_NAME is not set. Run scripts/create_foundry_agent.py "
            "and set FOUNDRY_AGENT_NAME on the Container App "
            "(see Post_Deployment_Steps.md §6/§7)."
        )
    return name


def _openai_client():
    """Cached OpenAI Responses client bound to the Foundry agent endpoint."""
    global _openai
    if _openai is None:
        _openai = _project_client().get_openai_client(agent_name=_agent_name())
    return _openai


# ---------------------------------------------------------------------------
# Local tool dispatch
# ---------------------------------------------------------------------------
# `nl2sql` and `chart_spec` are declared as Function tools on the agent
# (see scripts/create_foundry_agent.py). The Responses API surfaces tool
# calls as `function_call` items; we execute them locally and submit the
# results as `function_call_output` items in the next request.
# `doc_search` is handled server-side by the AzureAISearchTool attached to
# the agent — we never see a function_call for it.

def _dispatch_tool(
    name: str,
    args: dict,
    last_rows: list | None = None,
    recent_results: list[dict[str, Any]] | None = None,
) -> str:
    """Execute a tool call locally. `last_rows` is the most recent nl2sql
    result and is auto-injected into chart_spec when the model forgets to
    pass `rows` (or passes []).
    """
    try:
        if name == "nl2sql":
            return json.dumps(run_nl2sql(args["sql"]))
        if name == "chart_spec":
            rows = args.get("rows") or []
            if not rows and recent_results:
                rows = _select_chart_rows(args, recent_results)
            if not rows and last_rows:
                log.info("chart_spec called with empty rows — auto-injecting %d rows from last nl2sql", len(last_rows))
                rows = last_rows
            return json.dumps(chart_spec(
                rows=rows,
                intent=args.get("intent", "bar"),
                x=args.get("x"),
                y=args.get("y"),
                color=args.get("color"),
                title=args.get("title"),
            ))
        if name == "dashboard_spec":
            datasets = _hydrate_dashboard_datasets(args.get("datasets") or [], recent_results or [])
            datasets = _ensure_occurrence_recent_records(
                datasets=datasets,
                domain=args.get("domain"),
                title=args.get("title"),
                focus=args.get("focus"),
            )
            return json.dumps(dashboard_spec(
                datasets=datasets,
                title=args.get("title"),
                domain=args.get("domain"),
                focus=args.get("focus"),
            ))
        if name == "doc_search":
            return json.dumps(doc_search(args.get("query", "")))
        return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return json.dumps({"error": str(e)})


def _extract_rows(nl2sql_output_json: str) -> list | None:
    """Pull a `rows`-shaped list from nl2sql JSON output, if present."""
    try:
        data = json.loads(nl2sql_output_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("rows", "data", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return None


def _extract_result_payload(nl2sql_output_json: str) -> dict[str, Any] | None:
    try:
        data = json.loads(nl2sql_output_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _row_keys(result: dict[str, Any]) -> set[str]:
    keys = set(result.get("columns") or [])
    rows = result.get("rows") or []
    if rows and isinstance(rows[0], dict):
        keys.update(rows[0].keys())
    return {str(key) for key in keys}


def _select_chart_rows(args: dict[str, Any], recent_results: list[dict[str, Any]]) -> list:
    requested_fields = [
        str(value) for value in (args.get("x"), args.get("y"), args.get("color")) if value
    ]
    best_rows: list = []
    best_score = -1
    for result in reversed(recent_results):
        rows = result.get("rows") or []
        keys = _row_keys(result)
        score = sum(1 for field in requested_fields if field in keys)
        if requested_fields and score == 0:
            continue
        if score > best_score:
            best_rows = rows
            best_score = score
    if best_rows:
        log.info("chart_spec called with empty rows — auto-selected %d rows from matching nl2sql result", len(best_rows))
    return best_rows


def _hydrate_dashboard_datasets(
    datasets: list[dict[str, Any]],
    recent_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    for dataset in datasets:
        if dataset.get("rows"):
            hydrated.append(dataset)
            continue

        name = str(dataset.get("name") or "").strip().lower()
        rows = _select_dashboard_rows(name, recent_results)
        if rows:
            log.info(
                "dashboard_spec dataset '%s' missing rows — auto-injecting %d rows from prior nl2sql results",
                name,
                len(rows),
            )
        hydrated.append({**dataset, "rows": rows})
    return hydrated


def _select_dashboard_rows(name: str, recent_results: list[dict[str, Any]]) -> list:
    expected_fields = {
        "overview": {"Active_Tracks", "Jamming_Index_Pct", "Integrity_Index_Pct", "Loss_Alert_Count", "Records_Analyzed"},
        "summary": {"Active_Tracks", "Jamming_Index_Pct", "Integrity_Index_Pct", "Loss_Alert_Count", "Records_Analyzed"},
        "tracks": {"Track_ID", "Callsign", "Conflict_Risk_Score"},
        "hotspots": {"Zone_ID", "Zone_Label", "Event_Count"},
        "alerts": {"Conflict_Alert", "Conflict_Pair", "Callsign"},
        "tactical_audit": {"Tactical_Audit_ID", "Composite_Risk_Score", "Intelligence_Summary"},
        "recent_records": {"Occurrence_ID", "Occurrence_Date", "Summary"},
    }
    expected = expected_fields.get(name, set())
    best_rows: list = []
    best_score = -1
    for result in reversed(recent_results):
        rows = result.get("rows") or []
        keys = _row_keys(result)
        score = len(expected & keys) if expected else 0
        if expected and score == 0:
            continue
        if score > best_score:
            best_rows = rows
            best_score = score
    return best_rows


def _ensure_occurrence_recent_records(
    datasets: list[dict[str, Any]],
    domain: Any,
    title: Any,
    focus: Any,
) -> list[dict[str, Any]]:
    activity_code = _dashboard_activity_code(domain, title, focus, datasets)
    if not activity_code:
        return datasets

    recent_index = -1
    for index, dataset in enumerate(datasets):
        name = str(dataset.get("name") or "").strip().lower().replace(" ", "_")
        if name == "recent_records":
            recent_index = index
            if dataset.get("rows"):
                return datasets
            break

    rows = _fetch_latest_occurrence_records(activity_code)
    if not rows:
        return datasets

    fallback_dataset = {
        "name": "recent_records",
        "title": f"Latest available {activity_code.lower()} records",
        "source_window": "latest_available",
        "note": f"No rows were returned for the last 12 months, so the dashboard widened to the latest available {activity_code.lower()} records.",
        "rows": rows,
    }
    log.info(
        "dashboard_spec recent_records empty for %s — backfilling %d latest available rows",
        activity_code,
        len(rows),
    )

    if recent_index >= 0:
        return [
            fallback_dataset if index == recent_index else dataset
            for index, dataset in enumerate(datasets)
        ]
    return [*datasets, fallback_dataset]


def _dashboard_activity_code(
    domain: Any,
    title: Any,
    focus: Any,
    datasets: list[dict[str, Any]],
) -> str | None:
    haystack = " ".join(
        [str(domain or ""), str(title or ""), str(focus or "")]
        + [str(dataset.get("name") or "") for dataset in datasets]
    ).lower()
    if "bird" in haystack:
        return "Bird Strike"
    if "runway" in haystack or "incursion" in haystack:
        return "Runway Incursion"
    return None


def _fetch_latest_occurrence_records(activity_code: str, limit: int = 10) -> list[dict[str, Any]]:
    query = f"""
SELECT TOP {limit}
    Occurrence_ID,
    Occurrence_Date,
    Location,
    Organisation_Name,
    Aircraft_Registration,
    Occurrence_Subtype,
    Finding_Level,
    Current_Status,
    Summary
FROM dbo.vw_SafetyIntel_Occurrences
WHERE Activity_Code = '{activity_code}'
ORDER BY Occurrence_Date DESC
""".strip()
    result = run_nl2sql(query)
    rows = result.get("rows") if isinstance(result, dict) else None
    return rows if isinstance(rows, list) else []


def _infer_occurrence_activity(message: str, recent_results: list[dict[str, Any]]) -> str | None:
    lowered = message.lower()
    if "bird" in lowered:
        return "Bird Strike"
    if "runway" in lowered or "incursion" in lowered:
        return "Runway Incursion"
    if "personnel" in lowered or "licence" in lowered or "pilot" in lowered or "ame" in lowered:
        return "Personnel Licences"
    if "aircraft" in lowered or "fleet" in lowered or "register" in lowered or "registry" in lowered:
        return "Aircraft Registry"
    if "aerodrome" in lowered or "airfield" in lowered or ("ground" in lowered and "handling" in lowered):
        return "Aerodrome Incidents"
    if "atc" in lowered or "air traffic" in lowered or "separation" in lowered:
        return "ATC Incidents"
    if "amo" in lowered or "audit" in lowered or "maintenance" in lowered:
        return "AMO Audit"

    for result in reversed(recent_results):
        rows = result.get("rows") or []
        if rows and isinstance(rows[0], dict):
            activity_code = rows[0].get("Activity_Code")
            if activity_code in {"Bird Strike", "Runway Incursion", "Personnel Licences", "Aircraft Registry", "Aerodrome Incidents", "ATC Incidents"}:
                return str(activity_code)
    return None


def _build_auto_dashboard_args(message: str, recent_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    activity_code = _infer_occurrence_activity(message, recent_results)
    if activity_code is None:
        return None

    domain = activity_code.lower().replace(" ", "_")
    datasets = []
    populated = 0
    for name in ("overview", "tracks", "hotspots", "alerts", "tactical_audit", "recent_records"):
        rows = _select_dashboard_rows(name, recent_results)
        if rows:
            populated += 1
        datasets.append({"name": name, "rows": rows})

    if populated < 3:
        fallback = _build_canonical_occurrence_dashboard_args(activity_code)
        if fallback is not None:
            log.info(
                "auto-dashboard using canonical %s fallback after sparse model-led datasets",
                activity_code,
            )
        return fallback

    if activity_code == "Bird Strike":
        title = "Bird Strike Operations Dashboard"
        focus = "Recent bird-strike review"
    elif activity_code == "Personnel Licences":
        title = "Personnel Licensing Dashboard"
        focus = "CAAS PLSD — Licence holder status and renewal tracking"
    elif activity_code == "Aircraft Registry":
        title = "Aircraft Registry Dashboard"
        focus = "Singapore Aircraft Register — fleet composition and maintenance status"
    elif activity_code == "Aerodrome Incidents":
        title = "Aerodrome Incident Dashboard"
        focus = "WSSS Changi Aerodrome — ground handling, FOD, lighting, and fuel-spill monitoring"
    elif activity_code == "ATC Incidents":
        title = "Air Traffic Incident Dashboard"
        focus = "SIN FIR — loss of separation, CNS failures, and coordination gaps"
    elif activity_code == "AMO Audit":
        title = "AMO Quality Audit Dashboard"
        focus = "Aircraft maintenance organisation audit results"
    else:
        title = "Runway Incursion Operations Dashboard"
        focus = "Runway incursion operational picture"

    datasets = _ensure_occurrence_recent_records(
        datasets=datasets,
        domain=domain,
        title=title,
        focus=focus,
    )
    return {
        "datasets": datasets,
        "title": title,
        "domain": domain,
        "focus": focus,
    }


def _has_minimum_dashboard_datasets(recent_results: list[dict[str, Any]]) -> bool:
    populated = 0
    for name in ("overview", "tracks", "hotspots", "alerts", "tactical_audit", "recent_records"):
        if _select_dashboard_rows(name, recent_results):
            populated += 1
    return populated >= 3


def _build_canonical_occurrence_dashboard_args(activity_code: str) -> dict[str, Any] | None:
    domain = activity_code.lower().replace(" ", "_")
    if activity_code == "Bird Strike":
        title = "Bird Strike Operations Dashboard"
        focus = "Recent bird-strike review"
    elif activity_code == "Runway Incursion":
        title = "Runway Incursion Operations Dashboard"
        focus = "Runway incursion operational picture"
    else:
        return None

    datasets = [
        {
            "name": "overview",
            "title": "Ops overview KPI",
            "rows": _run_dashboard_query(
                f"""
SELECT TOP 1
    Activity_Code,
    Active_Tracks,
    Jamming_Index_Pct,
    Integrity_Index_Pct,
    Loss_Alert_Count,
    Records_Analyzed,
    Primary_Location
FROM dbo.vw_SafetyIntel_OccurrenceOpsOverview
WHERE Activity_Code = '{activity_code}'
"""
            ),
        },
        {
            "name": "tracks",
            "title": "Active/most recent tracks",
            "rows": _run_dashboard_query(
                f"""
SELECT TOP 12
    Track_ID,
    Snapshot_Timestamp,
    Activity_Code,
    AWI,
    Organisation_Name,
    Callsign,
    Tail_ID,
    Location,
    Latitude,
    Longitude,
    Heading_Deg,
    Ground_Speed_Kts,
    Flight_Level,
    Integrity_Index_Pct,
    Jamming_Index_Pct,
    Conflict_Risk_Score,
    Conflict_Alert,
    Conflict_Pair,
    Current_Status
FROM dbo.vw_SafetyIntel_OccurrenceOps
WHERE Activity_Code = '{activity_code}'
ORDER BY Conflict_Risk_Score DESC, Snapshot_Timestamp DESC
"""
            ),
        },
        {
            "name": "hotspots",
            "title": "Hotspot zones",
            "rows": _run_dashboard_query(
                f"""
SELECT TOP 12
    Zone_ID,
    Activity_Code,
    Location,
    Zone_Label,
    Center_Latitude,
    Center_Longitude,
    Event_Count,
    Severity_Band,
    Loss_Alert_Count,
    Integrity_Index_Pct,
    Jamming_Index_Pct
FROM dbo.vw_SafetyIntel_OccurrenceHotspots
WHERE Activity_Code = '{activity_code}'
ORDER BY Event_Count DESC, Zone_Label ASC
"""
            ),
        },
        {
            "name": "alerts",
            "title": "Live alert cues",
            "rows": _run_dashboard_query(
                f"""
SELECT TOP 12
    Callsign,
    Tail_ID,
    Flight_Level,
    Ground_Speed_Kts,
    Conflict_Risk_Score,
    Conflict_Alert,
    Conflict_Pair
FROM dbo.vw_SafetyIntel_OccurrenceOps
WHERE Activity_Code = '{activity_code}'
  AND Conflict_Alert IS NOT NULL
ORDER BY Conflict_Risk_Score DESC, Snapshot_Timestamp DESC
"""
            ),
        },
        {
            "name": "tactical_audit",
            "title": "Tactical audit note",
            "rows": _run_dashboard_query(
                f"""
SELECT TOP 1
    Tactical_Audit_ID,
    Activity_Code,
    Track_ID,
    Tail_ID,
    Composite_Risk_Score,
    Intelligence_Summary,
    Action_1,
    Action_2,
    Action_3
FROM dbo.vw_SafetyIntel_TacticalAudit
WHERE Activity_Code = '{activity_code}'
ORDER BY Composite_Risk_Score DESC
"""
            ),
        },
        {
            "name": "recent_records",
            "title": "Recent occurrence records",
            "rows": _run_dashboard_query(
                f"""
SELECT TOP 12
    Occurrence_ID,
    Occurrence_Date,
    Location,
    Organisation_Name,
    Aircraft_Registration,
    Occurrence_Subtype,
    Finding_Level,
    Current_Status,
    Summary
FROM dbo.vw_SafetyIntel_Occurrences
WHERE Activity_Code = '{activity_code}'
  AND Occurrence_Date >= DATEADD(MONTH, -12, CAST(GETDATE() AS DATE))
ORDER BY Occurrence_Date DESC
"""
            ),
        },
    ]

    if sum(1 for dataset in datasets if dataset["rows"]) < 3:
        return None

    datasets = _ensure_occurrence_recent_records(
        datasets=datasets,
        domain=domain,
        title=title,
        focus=focus,
    )
    return {
        "datasets": datasets,
        "title": title,
        "domain": domain,
        "focus": focus,
    }


def _run_dashboard_query(query: str) -> list[dict[str, Any]]:
    result = run_nl2sql(query.strip())
    rows = result.get("rows") if isinstance(result, dict) else None
    return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# Streaming chat
# ---------------------------------------------------------------------------

async def run_chat_stream(session_id: str, message: str) -> AsyncIterator[dict]:
    """Send a user message to the Foundry v2 agent; yield event dicts.

    Event shapes::

        {"type": "tool_call",   "data": {"name": "...", "arguments": {...}}}
        {"type": "tool_result", "data": {"name": "...", "output": "..."}}
        {"type": "final",       "data": "<assistant text>"}
        {"type": "error",       "data": "<message>"}
    """
    client = _openai_client()
    session_anchor = _sessions.get(session_id)
    # Track the most recent nl2sql rows so we can auto-fill chart_spec.rows
    # if the model is lazy and passes []. Spans across the whole loop.
    last_rows: list | None = None
    recent_results: list[dict[str, Any]] = []
    artifact_emitted = False
    dashboard_emitted = False
    deferred_ui_events: list[tuple[dict[str, Any], dict[str, Any]]] = []
    nl2sql_call_count = 0

    # First turn for this session: pass plain user message.
    # Subsequent turns / tool-call followups: pass `previous_response_id`.
    request_input: list[dict] | str = message
    create_kwargs: dict = {"input": request_input}
    if session_anchor:
        create_kwargs["previous_response_id"] = session_anchor

    # Loop: each iteration is one Responses round-trip. We keep going as long
    # as the model emits function_calls that we can satisfy locally.
    while True:
        try:
            response = client.responses.create(**create_kwargs)
        except Exception as e:  # noqa: BLE001
            log.exception("responses.create failed")
            yield {"type": "error", "data": str(e)}
            return

        # Collect any function_call items in this response's output.
        function_calls = [
            item for item in (response.output or [])
            if getattr(item, "type", None) == "function_call"
        ]

        if not function_calls:
            _sessions[session_id] = response.id
            if not dashboard_emitted:
                auto_dashboard_args = _build_auto_dashboard_args(message, recent_results)
                if auto_dashboard_args is not None:
                    yield {"type": "tool_call", "data": {"name": "dashboard_spec", "arguments": auto_dashboard_args}}
                    auto_dashboard = _dispatch_tool(
                        "dashboard_spec",
                        auto_dashboard_args,
                        last_rows=last_rows,
                        recent_results=recent_results,
                    )
                    artifact_emitted = True
                    dashboard_emitted = True
                    yield {"type": "tool_result", "data": {"name": "dashboard_spec", "output": auto_dashboard}}
                    while deferred_ui_events:
                        deferred_call, deferred_result = deferred_ui_events.pop(0)
                        yield deferred_call
                        yield deferred_result
                elif deferred_ui_events:
                    while deferred_ui_events:
                        deferred_call, deferred_result = deferred_ui_events.pop(0)
                        yield deferred_call
                        yield deferred_result

            # No more tool work — emit the assistant text and finish.
            text = _extract_text(response)
            if text:
                yield {"type": "final", "data": text}
            return

        # Execute each function_call locally and stage outputs for the next round.
        tool_outputs: list[dict] = []
        for call in function_calls:
            name = call.name
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            defer_chart_ui = _should_defer_chart_ui(
                name=name,
                message=message,
                previous_response_id=session_anchor,
                dashboard_emitted=dashboard_emitted,
                recent_results=recent_results,
            )
            tool_call_event = {"type": "tool_call", "data": {"name": name, "arguments": args}}
            if not defer_chart_ui:
                yield tool_call_event

            result = _dispatch_tool(name, args, last_rows=last_rows, recent_results=recent_results)

            if name in {"chart_spec", "dashboard_spec"}:
                artifact_emitted = True
            if name == "dashboard_spec":
                dashboard_emitted = True

            # Cache nl2sql rows for any chart_spec call that follows.
            if name == "nl2sql":
                nl2sql_call_count += 1
                rows = _extract_rows(result)
                if rows is not None:
                    last_rows = rows
                payload = _extract_result_payload(result)
                if payload is not None:
                    recent_results.append(payload)
                    recent_results = recent_results[-12:]

            tool_result_event = {"type": "tool_result", "data": {"name": name, "output": result}}
            if defer_chart_ui:
                deferred_ui_events.append((tool_call_event, tool_result_event))
            else:
                yield tool_result_event

            if name == "dashboard_spec":
                while deferred_ui_events:
                    deferred_call, deferred_result = deferred_ui_events.pop(0)
                    yield deferred_call
                    yield deferred_result

            tool_outputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": result,
            })

        if _should_short_circuit_occurrence_dashboard(
            message=message,
            dashboard_emitted=dashboard_emitted,
            nl2sql_call_count=nl2sql_call_count,
        ):
            # We are about to stop locally without posting `tool_outputs` back to
            # Foundry, so keep the last completed response as the session anchor.
            if session_anchor:
                _sessions[session_id] = session_anchor
            else:
                _sessions.pop(session_id, None)
            auto_dashboard_args = _build_auto_dashboard_args(message, recent_results)
            if auto_dashboard_args is not None:
                yield {"type": "tool_call", "data": {"name": "dashboard_spec", "arguments": auto_dashboard_args}}
                auto_dashboard = _dispatch_tool(
                    "dashboard_spec",
                    auto_dashboard_args,
                    last_rows=last_rows,
                    recent_results=recent_results,
                )
                artifact_emitted = True
                dashboard_emitted = True
                yield {"type": "tool_result", "data": {"name": "dashboard_spec", "output": auto_dashboard}}
                while deferred_ui_events:
                    deferred_call, deferred_result = deferred_ui_events.pop(0)
                    yield deferred_call
                    yield deferred_result
                yield {"type": "final", "data": _summarize_dashboard_output(auto_dashboard)}
                return

        # Next round: feed outputs back, anchored on this response.
        create_kwargs = {
            "input": tool_outputs,
            "previous_response_id": response.id,
        }


def _extract_text(response) -> str:
    """Concatenate output_text parts from a Responses API response."""
    # Prefer the convenience accessor when available.
    text = getattr(response, "output_text", None)
    if text:
        return text

    parts: list[str] = []
    for item in response.output or []:
        if getattr(item, "type", None) != "message":
            continue
        for c in getattr(item, "content", None) or []:
            if getattr(c, "type", None) == "output_text":
                t = getattr(c, "text", None)
                if t:
                    parts.append(t)
    return "\n".join(parts)


def _should_defer_chart_ui(
    name: str,
    message: str,
    previous_response_id: str | None,
    dashboard_emitted: bool,
    recent_results: list[dict[str, Any]],
) -> bool:
    if name != "chart_spec" or previous_response_id is not None or dashboard_emitted:
        return False
    if _infer_occurrence_activity(message, recent_results) != "Bird Strike":
        return False
    return True


def _should_short_circuit_occurrence_dashboard(
    message: str,
    dashboard_emitted: bool,
    nl2sql_call_count: int,
) -> bool:
    if dashboard_emitted or nl2sql_call_count < 5:
        return False
    return _infer_occurrence_activity(message, []) in {"Bird Strike", "Runway Incursion", "Personnel Licences", "Aircraft Registry", "Aerodrome Incidents", "ATC Incidents", "AMO Audit"}


def _summarize_dashboard_output(output_json: str) -> str:
    try:
        payload = json.loads(output_json)
    except (json.JSONDecodeError, TypeError):
        return "Operational dashboard is ready for review."
    if not isinstance(payload, dict):
        return "Operational dashboard is ready for review."

    title = str(payload.get("title") or "Operational dashboard")
    metrics = payload.get("metrics") or []
    metric_lookup = {
        str(item.get("label") or ""): str(item.get("value") or "")
        for item in metrics
        if isinstance(item, dict)
    }
    hotspots = payload.get("hotspots") or []
    hotspot = hotspots[0] if hotspots and isinstance(hotspots[0], dict) else None
    tactical = payload.get("tactical_audit") if isinstance(payload.get("tactical_audit"), dict) else None
    recent_records = payload.get("recent_records") or []
    records_label = "occurrence rows" if recent_records else "dashboard evidence rows"
    focus_location = _summary_focus_location(hotspot, recent_records, tactical)
    opening = (
        f"{title} is displayed with the current operational picture centered on **{focus_location}**. "
        f"**{metric_lookup.get('Vectors', '0')}** active vectors, **{metric_lookup.get('LOS alerts', '0')}** loss-alert cues, "
        f"**Integrity {metric_lookup.get('Integrity', '0%')}**, and **Jamming {metric_lookup.get('Jamming', '0%')}** are on watch."
    )

    detail_parts: list[str] = []
    if hotspot:
        detail_parts.append(
            f"The top-ranked hotspot is **{hotspot.get('label', 'unknown')}** with **{_summary_hotspot_count(hotspot)}** events"
        )
    if tactical:
        detail_parts.append(
            f"the tactical score is **{_summary_int(tactical.get('composite_risk_score'))}** for **{tactical.get('tail_id', 'N/A')}**"
        )
    if recent_records:
        detail_parts.append(
            f"the view is anchored on **{len(recent_records)}** recent {records_label}"
        )
    elif metric_lookup.get("Records analyzed"):
        detail_parts.append(
            f"the view is anchored on **{metric_lookup.get('Records analyzed', '0')}** {records_label}"
        )

    paragraphs = [opening]
    if detail_parts:
        detail_sentence = "; ".join(detail_parts)
        paragraphs.append(detail_sentence[:1].upper() + detail_sentence[1:] + ".")

    tactical_summary = _clean_summary_text((tactical or {}).get("summary"))
    if tactical_summary:
        paragraphs.append(tactical_summary)

    actions = [
        _clean_summary_text(action)
        for action in ((tactical or {}).get("actions") or [])
        if _clean_summary_text(action)
    ]
    if actions:
        paragraphs.append("Recommended actions:\n" + "\n".join(f"- {action}" for action in actions[:3]))

    lookback_note = _clean_summary_text(payload.get("lookback_note"))
    if lookback_note:
        paragraphs.append(f"Lookback note: {lookback_note}")

    sources = _summary_sources(payload.get("datasets"), payload.get("domain"))
    if sources:
        paragraphs.append("Sources:\n" + "\n".join(f"- {source}" for source in sources))

    return "\n\n".join(part for part in paragraphs if part)


def _summary_focus_location(
    hotspot: dict[str, Any] | None,
    recent_records: list[dict[str, Any]],
    tactical: dict[str, Any] | None,
) -> str:
    if recent_records and isinstance(recent_records[0], dict):
        location = recent_records[0].get("Location") or recent_records[0].get("location")
        if location:
            return str(location)
    if hotspot and hotspot.get("label"):
        return str(hotspot["label"])
    if tactical and tactical.get("tail_id"):
        return str(tactical["tail_id"])
    return "the monitored area"


def _summary_hotspot_count(hotspot: dict[str, Any]) -> str:
    try:
        return f"{float(hotspot.get('count', 0)):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(hotspot.get("count", 0))


def _summary_int(value: Any) -> str:
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value or 0)


def _clean_summary_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = " ".join(str(value).split())
    return text.strip()


def _summary_sources(datasets: Any, domain: str | None = None) -> list[str]:
    if not isinstance(datasets, list):
        return []
    dataset_to_view = {
        "overview": "dbo.vw_SafetyIntel_OccurrenceOpsOverview",
        "tracks": "dbo.vw_SafetyIntel_OccurrenceOps",
        "alerts": "dbo.vw_SafetyIntel_OccurrenceOps",
        "hotspots": "dbo.vw_SafetyIntel_OccurrenceHotspots",
        "tactical_audit": "dbo.vw_SafetyIntel_TacticalAudit",
        "recent_records": "dbo.vw_SafetyIntel_Occurrences",
    }
    domain_views = {
        "personnel_licences": "dbo.vw_SafetyIntel_PersonnelLicences",
        "aircraft_registry": "dbo.vw_SafetyIntel_AircraftRegister",
        "aerodrome_incidents": "dbo.vw_SafetyIntel_AerodromeIncidents",
        "atc_incidents": "dbo.vw_SafetyIntel_ATCIncidents",
        "cross_domain": "dbo.vw_SafetyIntel_CrossDomainMetrics",
    }
    sources: list[str] = []
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        name = str(dataset.get("name") or "").strip().lower()
        row_count = dataset.get("row_count")
        if isinstance(row_count, (int, float)) and row_count <= 0:
            continue
        # Domain-specific view takes priority
        view_name = None
        if domain and name in ("overview", "tracks", "alerts") and domain in domain_views:
            view_name = domain_views.get(domain)
        if not view_name:
            view_name = dataset_to_view.get(name)
        if view_name and view_name not in sources:
            sources.append(view_name)
    return sources
