"""Local demo-mode chat stream for UI preview without Foundry.

Enable with:
    SAFETY_INTEL_DEMO_MODE=1
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from tools import chart_spec, dashboard_spec


RUNWAY_OVERVIEW = [{
    "Activity_Code": "Runway Incursion",
    "Active_Tracks": 7,
    "Jamming_Index_Pct": 3,
    "Integrity_Index_Pct": 94,
    "Loss_Alert_Count": 1,
    "Records_Analyzed": 30,
    "Primary_Location": "WSSS",
}]

RUNWAY_TRACKS = [
    {"Track_ID": "OPS-RI-001", "Callsign": "TGW598", "Tail_ID": "9V-761", "Location": "WSSS", "Latitude": 1.3541, "Longitude": 103.9871, "Heading_Deg": 266, "Ground_Speed_Kts": 265, "Flight_Level": "FL95", "Integrity_Index_Pct": 96, "Jamming_Index_Pct": 3, "Conflict_Risk_Score": 25, "Conflict_Alert": "Conflict alert", "Conflict_Pair": "SIA212", "Current_Status": "Monitoring"},
    {"Track_ID": "OPS-RI-002", "Callsign": "TGW427", "Tail_ID": "9V-427", "Location": "WSSS", "Latitude": 1.3508, "Longitude": 103.9921, "Heading_Deg": 281, "Ground_Speed_Kts": 246, "Flight_Level": "FL90", "Integrity_Index_Pct": 94, "Jamming_Index_Pct": 4, "Conflict_Risk_Score": 18, "Conflict_Alert": "Vehicle crossing active runway", "Conflict_Pair": "TGW598", "Current_Status": "Monitoring"},
    {"Track_ID": "OPS-RI-003", "Callsign": "TGW676", "Tail_ID": "9V-676", "Location": "WSSS", "Latitude": 1.3487, "Longitude": 103.9981, "Heading_Deg": 302, "Ground_Speed_Kts": 480, "Flight_Level": "FL340", "Integrity_Index_Pct": 97, "Jamming_Index_Pct": 2, "Conflict_Risk_Score": 12, "Conflict_Alert": None, "Conflict_Pair": None, "Current_Status": "Monitoring"},
]

RUNWAY_HOTSPOTS = [
    {"Zone_ID": "GRID-RI-01", "Zone_Label": "South runway crossing", "Location": "WSSS", "Center_Latitude": 1.3519, "Center_Longitude": 103.9912, "Event_Count": 8, "Severity_Band": "critical", "Loss_Alert_Count": 1, "Integrity_Index_Pct": 95, "Jamming_Index_Pct": 3},
    {"Zone_ID": "GRID-RI-02", "Zone_Label": "Taxi lane cluster", "Location": "WSSS", "Center_Latitude": 1.3492, "Center_Longitude": 103.9898, "Event_Count": 6, "Severity_Band": "watch", "Loss_Alert_Count": 0, "Integrity_Index_Pct": 94, "Jamming_Index_Pct": 4},
]

RUNWAY_TACTICAL = [{
    "Tactical_Audit_ID": "TACT-RI-001",
    "Track_ID": "OPS-RI-001",
    "Tail_ID": "9V-761",
    "Composite_Risk_Score": 25,
    "Intelligence_Summary": "Vehicle crossing risk remains concentrated on the southern runway transition with one active conflict pair still under watch.",
    "Action_1": "Reconfirm runway access control and escort procedure for all ground vehicles.",
    "Action_2": "Issue a targeted radio phraseology reminder to tug and maintenance crews.",
    "Action_3": "Review hold-point signage and low-visibility taxi brief for the current shift.",
}]

RUNWAY_RECENT = [
    {"Occurrence_Date": "2025-01-22", "Location": "WSSS", "Organisation_Name": "Hawker Pacific Airservices Pte Ltd", "Occurrence_Subtype": "Unauthorised entry", "Finding_Level": "Level 2", "Current_Status": "In-progress", "Summary": "Ground vehicle entered active runway without ATC clearance; investigation ongoing."},
    {"Occurrence_Date": "2024-12-11", "Location": "WSSS", "Organisation_Name": "Changi Avionics Services Pte Ltd", "Occurrence_Subtype": "Vehicle on active runway", "Finding_Level": "Level 2", "Current_Status": "Closed", "Summary": "Maintenance van entered active runway; SOP gap closed with new radio procedure."},
    {"Occurrence_Date": "2024-06-08", "Location": "VHHH", "Organisation_Name": "SIAEC Line Maintenance (HKG)", "Occurrence_Subtype": "Taxi past hold point", "Finding_Level": "Level 2", "Current_Status": "Closed", "Summary": "Aircraft taxied past CAT II hold point; crew briefed on low-visibility procedures."},
]

BIRD_TREND = [
    {"Occurrence_Month": "2025-05", "Bird_Strike_Count": 2},
    {"Occurrence_Month": "2025-06", "Bird_Strike_Count": 3},
    {"Occurrence_Month": "2025-07", "Bird_Strike_Count": 3},
    {"Occurrence_Month": "2025-08", "Bird_Strike_Count": 4},
    {"Occurrence_Month": "2025-09", "Bird_Strike_Count": 5},
    {"Occurrence_Month": "2025-10", "Bird_Strike_Count": 4},
    {"Occurrence_Month": "2025-11", "Bird_Strike_Count": 3},
    {"Occurrence_Month": "2025-12", "Bird_Strike_Count": 4},
    {"Occurrence_Month": "2026-01", "Bird_Strike_Count": 6},
    {"Occurrence_Month": "2026-02", "Bird_Strike_Count": 5},
    {"Occurrence_Month": "2026-03", "Bird_Strike_Count": 7},
    {"Occurrence_Month": "2026-04", "Bird_Strike_Count": 6},
]

BIRD_HOTSPOTS = [
    {"Zone_ID": "GRID-BS-01", "Zone_Label": "Approach wildlife corridor", "Location": "WSSS", "Center_Latitude": 1.3574, "Center_Longitude": 103.9779, "Event_Count": 7, "Severity_Band": "critical", "Loss_Alert_Count": 0, "Integrity_Index_Pct": 95, "Jamming_Index_Pct": 2},
    {"Zone_ID": "GRID-BS-02", "Zone_Label": "Coastal climb-out corridor", "Location": "WSSS", "Center_Latitude": 1.3602, "Center_Longitude": 103.9836, "Event_Count": 5, "Severity_Band": "watch", "Loss_Alert_Count": 0, "Integrity_Index_Pct": 94, "Jamming_Index_Pct": 3},
]

BIRD_TRACKS = [
    {"Track_ID": "OPS-BS-001", "Callsign": "BIR231", "Tail_ID": "9V-STF", "Location": "WSSS", "Latitude": 1.3592, "Longitude": 103.9752, "Heading_Deg": 192, "Ground_Speed_Kts": 154, "Flight_Level": "FL70", "Integrity_Index_Pct": 95, "Jamming_Index_Pct": 2, "Conflict_Risk_Score": 14, "Conflict_Alert": "Wildlife cluster ahead", "Conflict_Pair": None, "Current_Status": "Open"},
    {"Track_ID": "OPS-BS-002", "Callsign": "BIR118", "Tail_ID": "9V-JAS", "Location": "WSSS", "Latitude": 1.3578, "Longitude": 103.9795, "Heading_Deg": 205, "Ground_Speed_Kts": 162, "Flight_Level": "FL82", "Integrity_Index_Pct": 94, "Jamming_Index_Pct": 3, "Conflict_Risk_Score": 16, "Conflict_Alert": "Multiple flock signatures", "Conflict_Pair": None, "Current_Status": "Open"},
]

BIRD_TACTICAL = [{
    "Tactical_Audit_ID": "TACT-BS-001",
    "Track_ID": "OPS-BS-002",
    "Tail_ID": "9V-JAS",
    "Composite_Risk_Score": 22,
    "Intelligence_Summary": "Bird-strike pressure remains concentrated on approach and climb-out corridors, with windshield and ingestion events overrepresented.",
    "Action_1": "Synchronise wildlife dispersal patrols with the morning arrival bank.",
    "Action_2": "Brief line inspectors to prioritise radome, windshield, and fan-blade checks after reports.",
    "Action_3": "Track repeat subtypes by location to separate random strikes from persistent habitat issues.",
}]

BIRD_RECENT = [
    {"Occurrence_Date": "2025-04-02", "Location": "KATL", "Organisation_Name": "Delta TechOps", "Occurrence_Subtype": "Engine fan damage", "Finding_Level": "Level 2", "Current_Status": "In-progress", "Summary": "Bird ingestion caused fan blade damage; engine removed for shop inspection."},
    {"Occurrence_Date": "2025-01-05", "Location": "OMAA", "Organisation_Name": "Etihad Engineering", "Occurrence_Subtype": "Cockpit window", "Finding_Level": "Level 3", "Current_Status": "In-progress", "Summary": "Bird strike on First Officer windshield during descent; inner-pane crack."},
    {"Occurrence_Date": "2024-07-01", "Location": "WIII", "Organisation_Name": "GMF AeroAsia Indonesia", "Occurrence_Subtype": "Lower fuselage", "Finding_Level": "OBS", "Current_Status": "Closed", "Summary": "Bird remains found on lower fuselage post-landing; no damage."},
]

BIRD_SUBTYPES = [
    {"Occurrence_Subtype": "Engine fan damage", "Bird_Strike_Count": 4},
    {"Occurrence_Subtype": "Cockpit window", "Bird_Strike_Count": 3},
    {"Occurrence_Subtype": "Windshield impact", "Bird_Strike_Count": 3},
    {"Occurrence_Subtype": "Lower fuselage", "Bird_Strike_Count": 2},
]

# ---------------------------------------------------------------------------
# AMO Audit mock data
# ---------------------------------------------------------------------------

AMO_OVERVIEW = [{
    "Organisation": "SIA Engineering Company Line Maintenance (WSSS)",
    "Audit_Type": "Scheduled Quality Assurance Audit",
    "Audit_Period": "2026 Q2",
    "Total_Findings": 12,
    "Critical_Findings": 1,
    "High_Findings": 3,
    "Medium_Findings": 5,
    "Low_Findings": 2,
    "Observations": 1,
    "Open_Findings": 2,
    "In_Progress_Findings": 4,
    "Closed_Findings": 6,
    "Audit_Scope": "Line maintenance records, tool control, technician certifications, parts traceability",
}]

AMO_FINDINGS = [
    {"Track_ID": "AMO-2026-001", "Callsign": "Avionics Workshop", "Tail_ID": "Open", "Flight_Level": "Torque wrench calibration overdue", "Severity_Band": "high", "Risk_Score": 72, "Due_Date": "2026-08-15"},
    {"Track_ID": "AMO-2026-002", "Callsign": "Engine Test Cell", "Tail_ID": "In Progress", "Flight_Level": "Fire suppression cert expired", "Severity_Band": "critical", "Risk_Score": 92, "Due_Date": "2026-07-30"},
    {"Track_ID": "AMO-2026-003", "Callsign": "Parts Warehouse", "Tail_ID": "In Progress", "Flight_Level": "Unsegregated unserviceable components", "Severity_Band": "high", "Risk_Score": 68, "Due_Date": "2026-08-01"},
    {"Track_ID": "AMO-2026-004", "Callsign": "Line Maintenance", "Tail_ID": "Closed", "Flight_Level": "Missing task card sign-off", "Severity_Band": "medium", "Risk_Score": 45, "Due_Date": "2026-07-10"},
    {"Track_ID": "AMO-2026-005", "Callsign": "Avionics Workshop", "Tail_ID": "Closed", "Flight_Level": "Oscilloscope calibration illegible", "Severity_Band": "medium", "Risk_Score": 38, "Due_Date": "2026-07-05"},
    {"Track_ID": "AMO-2026-006", "Callsign": "Technical Records", "Tail_ID": "In Progress", "Flight_Level": "AD/SB compliance log incomplete", "Severity_Band": "high", "Risk_Score": 75, "Due_Date": "2026-08-10"},
    {"Track_ID": "AMO-2026-007", "Callsign": "Line Maintenance", "Tail_ID": "In Progress", "Flight_Level": "Hydraulic mule hose chafing", "Severity_Band": "medium", "Risk_Score": 42, "Due_Date": "2026-07-28"},
    {"Track_ID": "AMO-2026-008", "Callsign": "Engine Test Cell", "Tail_ID": "Closed", "Flight_Level": "Test cell log unapproved cross-outs", "Severity_Band": "medium", "Risk_Score": 50, "Due_Date": "2026-07-02"},
    {"Track_ID": "AMO-2026-009", "Callsign": "Parts Warehouse", "Tail_ID": "Closed", "Flight_Level": "Shelf-life tracking not updated", "Severity_Band": "medium", "Risk_Score": 55, "Due_Date": "2026-06-28"},
    {"Track_ID": "AMO-2026-010", "Callsign": "Technical Records", "Tail_ID": "Closed", "Flight_Level": "Digital signature cert expiring", "Severity_Band": "low", "Risk_Score": 20, "Due_Date": "2026-07-01"},
    {"Track_ID": "AMO-2026-011", "Callsign": "Line Maintenance", "Tail_ID": "Closed", "Flight_Level": "Tug battery undervoltage", "Severity_Band": "low", "Risk_Score": 18, "Due_Date": "2026-06-25"},
    {"Track_ID": "AMO-2026-012", "Callsign": "Avionics Workshop", "Tail_ID": "Closed", "Flight_Level": "ESD mat resistance exceeds threshold", "Severity_Band": "observation", "Risk_Score": 8, "Due_Date": "2026-06-20"},
]

AMO_HOTSPOTS = [
    {"Zone_Label": "Avionics Workshop", "Event_Count": 3, "Severity_Band": "critical"},
    {"Zone_Label": "Line Maintenance", "Event_Count": 3, "Severity_Band": "watch"},
    {"Zone_Label": "Engine Test Cell", "Event_Count": 2, "Severity_Band": "critical"},
    {"Zone_Label": "Parts Warehouse", "Event_Count": 2, "Severity_Band": "watch"},
    {"Zone_Label": "Technical Records", "Event_Count": 2, "Severity_Band": "watch"},
]

AMO_ALERTS = [
    {"Callsign": "Engine Test Cell", "Flight_Level": "Fire suppression cert expired — E190 and B737NG test runs at risk", "Risk_Score": 92},
    {"Callsign": "Avionics Workshop", "Flight_Level": "Uncertified torque tools in active use on A320 brake assemblies", "Risk_Score": 72},
    {"Callsign": "Technical Records", "Flight_Level": "AD/SB compliance gap — 2 A320 aircraft with missing mandatory mod records", "Risk_Score": 75},
    {"Callsign": "Parts Warehouse", "Flight_Level": "Unsegregated unserviceable parts could re-enter supply chain", "Risk_Score": 68},
]

AMO_TACTICAL = [{
    "Tactical_Audit_ID": "AUDIT-AMO-2026-Q2",
    "Tail_ID": "SIA Engineering-WSSS",
    "Composite_Risk_Score": 52,
    "Intelligence_Summary": "Q2 quality audit reveals systemic documentation and tool-control gaps concentrated in Avionics Workshop and Line Maintenance. The single critical finding (expired fire-suppression cert in the test cell) requires immediate escalation. Positive closure rate on low/observation items shows corrective-action process is functioning for routine issues, but High and Critical items persist longer than the 30-day target.",
    "Action_1": "Escalate AMO-2026-002 (fire suppression cert) to Facility Manager with daily tracking — grounds all engine test runs until resolved.",
    "Action_2": "Conduct a stand-down tool-control briefing in Avionics Workshop by end of week, covering calibration tracking and ESD mat compliance.",
    "Action_3": "Reconcile AD/SB compliance log for 9V-SMQ and 9V-SMR against OEM service bulletins within 10 business days.",
}]

AMO_RECENT = [
    {"Date": "2026-07-18", "Department": "Engine Test Cell", "Finding": "Fire suppression cert expired", "Severity": "Critical", "Status": "In Progress", "Summary": "Hydrostatic test cert for FM-200 system in test cell #2 expired 2026-07-04; certificate renewal submitted to Bureau Veritas, ETA 2026-07-30."},
    {"Date": "2026-07-15", "Department": "Avionics Workshop", "Finding": "Torque wrench calibration overdue", "Severity": "High", "Status": "Open", "Summary": "Two Snap-on torque wrenches (S/N TQ-441, TQ-442) past 90-day recertification; replacement tools requisitioned but not yet received."},
    {"Date": "2026-07-12", "Department": "Technical Records", "Finding": "AD/SB compliance gap", "Severity": "High", "Status": "In Progress", "Summary": "Three entries missing from AD compliance binder for A320 family covering February 2026; records officer reviewing logbook archives."},
    {"Date": "2026-07-08", "Department": "Parts Warehouse", "Finding": "Unsegregated components", "Severity": "High", "Status": "In Progress", "Summary": "Three unserviceable actuators found in serviceable bins during spot check; quarantine initiated and receiving inspection procedure reinforced."},
    {"Date": "2026-07-02", "Department": "Line Maintenance", "Finding": "Missing task card sign-off", "Severity": "Medium", "Status": "Closed", "Summary": "Task card CA-4412-B for 9V-SMU night check signed off retrospectively after audit identified the gap; shift lead counselled on documentation discipline."},
]

AMO_FINDINGS_BY_SEVERITY = [
    {"Severity": "Critical", "Count": 1},
    {"Severity": "High", "Count": 3},
    {"Severity": "Medium", "Count": 5},
    {"Severity": "Low", "Count": 2},
    {"Severity": "Observation", "Count": 1},
]

AMO_FINDINGS_BY_DEPT = [
    {"Department": "Avionics Workshop", "Open": 1, "In_Progress": 0, "Closed": 2},
    {"Department": "Line Maintenance", "Open": 0, "In_Progress": 1, "Closed": 2},
    {"Department": "Engine Test Cell", "Open": 0, "In_Progress": 1, "Closed": 1},
    {"Department": "Parts Warehouse", "Open": 0, "In_Progress": 1, "Closed": 1},
    {"Department": "Technical Records", "Open": 0, "In_Progress": 1, "Closed": 1},
]


async def run_mock_chat_stream(session_id: str, message: str) -> AsyncIterator[dict]:
    del session_id
    text = message.strip().lower()

    if "runway incursion" in text:
        async for event in _runway_dashboard_events():
            yield event
        return

    if "bird strike" in text:
        async for event in _bird_dashboard_events():
            yield event
        return

    if "maintenance" in text or "amo" in text or "audit" in text:
        async for event in _amo_audit_dashboard_events():
            yield event
        return

    yield {
        "type": "final",
        "data": "Demo mode is active. Try one of these prompts: 'Show me the runway incursion dashboard' or 'Analyze recent bird strike'.",
    }


async def _runway_dashboard_events() -> AsyncIterator[dict]:
    dashboard = dashboard_spec(
        datasets=[
            {"name": "overview", "rows": RUNWAY_OVERVIEW},
            {"name": "tracks", "rows": RUNWAY_TRACKS},
            {"name": "hotspots", "rows": RUNWAY_HOTSPOTS},
            {"name": "tactical_audit", "rows": RUNWAY_TACTICAL},
            {"name": "recent_records", "rows": RUNWAY_RECENT},
        ],
        title="Runway Incursion Operations Dashboard",
        domain="runway_incursion",
        focus="Runway incursion control-tower view",
    )
    yield {"type": "tool_call", "data": {"name": "dashboard_spec", "arguments": {"domain": "runway_incursion"}}}
    await asyncio.sleep(0)
    yield {"type": "tool_result", "data": {"name": "dashboard_spec", "output": json.dumps(dashboard)}}
    await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "- One active conflict pair remains concentrated on the WSSS south-runway crossing corridor.\n- Telemetry quality is stable, but vehicle-crossing and phraseology gaps still dominate the open risk picture.\n- Recent records show the same runway-transition pattern repeating across tug, maintenance-vehicle, and taxi-hold events.\n\nSources:\n- vw_SafetyIntel_OccurrenceOpsOverview\n- vw_SafetyIntel_OccurrenceOps\n- vw_SafetyIntel_OccurrenceHotspots\n- vw_SafetyIntel_TacticalAudit\n- vw_SafetyIntel_Occurrences",
    }


async def _bird_dashboard_events() -> AsyncIterator[dict]:
    trend = chart_spec(BIRD_TREND, intent="line", x="Occurrence_Month", y="Bird_Strike_Count", title="Bird strikes by month (last 12 months)")
    breakdown = chart_spec(BIRD_SUBTYPES, intent="bar", x="Occurrence_Subtype", y="Bird_Strike_Count", title="Bird strike subtypes")
    dashboard = dashboard_spec(
        datasets=[
            {"name": "tracks", "rows": BIRD_TRACKS},
            {"name": "hotspots", "rows": BIRD_HOTSPOTS},
            {"name": "tactical_audit", "rows": BIRD_TACTICAL},
            {"name": "recent_records", "rows": BIRD_RECENT},
        ],
        title="Bird Strike Operations Dashboard",
        domain="bird_strike",
        focus="Recent bird-strike review",
    )
    for name, output in (
        ("chart_spec", trend),
        ("chart_spec", breakdown),
        ("dashboard_spec", dashboard),
    ):
        yield {"type": "tool_call", "data": {"name": name, "arguments": {"demo": True}}}
        await asyncio.sleep(0)
        yield {"type": "tool_result", "data": {"name": name, "output": json.dumps(output)}}
        await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "- Bird-strike counts are trending upward into the latest quarter, with the highest pressure in the most recent two months.\n- Engine, windshield, and cockpit-window events remain the dominant subtype pattern.\n- Current hotspots cluster around WSSS approach and climb-out corridors, which matches the tactical note's mitigation focus.\n\nSources:\n- vw_SafetyIntel_Occurrences\n- vw_SafetyIntel_OccurrenceHotspots\n- vw_SafetyIntel_OccurrenceOps\n- vw_SafetyIntel_TacticalAudit",
    }


async def _amo_audit_dashboard_events() -> AsyncIterator[dict]:
    severity_chart = chart_spec(AMO_FINDINGS_BY_SEVERITY, intent="bar", x="Severity", y="Count", title="Audit findings by severity")
    dept_chart = chart_spec(
        AMO_FINDINGS_BY_DEPT,
        intent="bar",
        x="Department",
        y="Open",
        color="Department",
        title="Open findings by department",
    )
    dashboard = dashboard_spec(
        datasets=[
            {"name": "overview", "rows": AMO_OVERVIEW},
            {"name": "tracks", "rows": AMO_FINDINGS},
            {"name": "hotspots", "rows": AMO_HOTSPOTS},
            {"name": "alerts", "rows": AMO_ALERTS},
            {"name": "tactical_audit", "rows": AMO_TACTICAL},
            {"name": "recent_records", "rows": AMO_RECENT},
        ],
        title="AMO Quality Audit Dashboard",
        domain="amo_audit",
        focus="SIA Engineering Line Maintenance — Q2 2026 audit results",
    )
    for name, output in (
        ("chart_spec", severity_chart),
        ("chart_spec", dept_chart),
        ("dashboard_spec", dashboard),
    ):
        yield {"type": "tool_call", "data": {"name": name, "arguments": {"demo": True}}}
        await asyncio.sleep(0)
        yield {"type": "tool_result", "data": {"name": name, "output": json.dumps(output)}}
        await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "- 1 critical finding (fire suppression cert expired) grounds all engine test cell operations until resolved.\n- Avionics Workshop and Line Maintenance account for 50% of all findings — systemic tool-control gaps identified.\n- 6 of 12 findings already closed; closure rate is healthy for low/medium items but High/Critical items average 28 days open, exceeding the 21-day KPI.\n- Top risk: unsegregated unserviceable parts in the warehouse could re-enter the supply chain if quarantine procedure is not reinforced.\n\nSources:\n- DM_TBL_SRG_AMO_ASSIGNED_PMI\n- DM_TBL_SRG_AMO_MOA\n- DM_TBL_SRG_AMO_TAM_LIST\n- DM_TBL_SRG_TACTICAL_AUDIT\n- DM_TBL_SRG_OCCURRENCES",
    }