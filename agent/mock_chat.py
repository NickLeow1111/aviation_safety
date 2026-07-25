"""Local demo-mode chat stream for UI preview without Foundry.

Enable with:
    SAFETY_INTEL_DEMO_MODE=1
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from tools import chart_spec, dashboard_spec


# ============================================================================
# Domain 1 — Runway Incursion (existing)
# ============================================================================

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

# ============================================================================
# Domain 2 — Bird Strike (existing)
# ============================================================================

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

# ============================================================================
# Domain 3 — AMO Audit (existing)
# ============================================================================

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

# ============================================================================
# Domain 4 — Personnel Licences (NEW)
# ============================================================================

PERSONNEL_OVERVIEW = [{
    "Category": "All Personnel Licences",
    "Total_Active_Licences": 2847,
    "Active_Pilots": 1634,
    "Active_AMEs": 852,
    "Active_Flight_Engineers": 127,
    "Active_Dispatchers": 234,
    "Active_Cabin_Crew_Licenced": 0,  # cabin crew not individually licenced under SARPs
    "Licences_Expiring_Within_90_Days": 89,
    "Licences_Suspended": 14,
    "Licences_Revoked_This_Year": 6,
    "Reporting_Period": "2026 Q2",
}]

PERSONNEL_LICENCES = [
    {"Licence_Type": "Airline Transport Pilot Licence (ATPL)", "Active": 894, "Pending_Renewal": 12, "Suspended": 3, "Expired_Last_90d": 5, "Avg_Age": 47, "Gender_M": 828, "Gender_F": 66},
    {"Licence_Type": "Commercial Pilot Licence (CPL)", "Active": 612, "Pending_Renewal": 18, "Suspended": 4, "Expired_Last_90d": 8, "Avg_Age": 34, "Gender_M": 543, "Gender_F": 69},
    {"Licence_Type": "Private Pilot Licence (PPL)", "Active": 128, "Pending_Renewal": 5, "Suspended": 1, "Expired_Last_90d": 3, "Avg_Age": 41, "Gender_M": 115, "Gender_F": 13},
    {"Licence_Type": "Aircraft Maintenance Engineer (AME) — B1", "Active": 412, "Pending_Renewal": 14, "Suspended": 3, "Expired_Last_90d": 7, "Avg_Age": 44, "Gender_M": 395, "Gender_F": 17},
    {"Licence_Type": "Aircraft Maintenance Engineer (AME) — B2", "Active": 356, "Pending_Renewal": 11, "Suspended": 2, "Expired_Last_90d": 4, "Avg_Age": 41, "Gender_M": 338, "Gender_F": 18},
    {"Licence_Type": "AME — B3 (Auxiliary Power Plant)", "Active": 84, "Pending_Renewal": 3, "Suspended": 1, "Expired_Last_90d": 1, "Avg_Age": 49, "Gender_M": 82, "Gender_F": 2},
    {"Licence_Type": "Flight Dispatcher Licence", "Active": 234, "Pending_Renewal": 6, "Suspended": 0, "Expired_Last_90d": 2, "Avg_Age": 39, "Gender_M": 198, "Gender_F": 36},
    {"Licence_Type": "Flight Engineer Licence", "Active": 127, "Pending_Renewal": 4, "Suspended": 0, "Expired_Last_90d": 3, "Avg_Age": 53, "Gender_M": 124, "Gender_F": 3},
]

PERSONNEL_EXPIRY_TREND = [
    {"Month": "2026-01", "Expiring_Licences": 22, "Renewals_Completed": 18, "Lapsed": 4},
    {"Month": "2026-02", "Expiring_Licences": 27, "Renewals_Completed": 24, "Lapsed": 3},
    {"Month": "2026-03", "Expiring_Licences": 31, "Renewals_Completed": 28, "Lapsed": 3},
    {"Month": "2026-04", "Expiring_Licences": 19, "Renewals_Completed": 16, "Lapsed": 3},
    {"Month": "2026-05", "Expiring_Licences": 35, "Renewals_Completed": 30, "Lapsed": 5},
    {"Month": "2026-06", "Expiring_Licences": 24, "Renewals_Completed": 22, "Lapsed": 2},
]

PERSONNEL_AGE_DEMOGRAPHIC = [
    {"Age_Bracket": "20–29", "Pilot_Count": 286, "AME_Count": 142, "Other_Count": 48, "Total": 476},
    {"Age_Bracket": "30–39", "Pilot_Count": 512, "AME_Count": 268, "Other_Count": 112, "Total": 892},
    {"Age_Bracket": "40–49", "Pilot_Count": 458, "AME_Count": 256, "Other_Count": 98, "Total": 812},
    {"Age_Bracket": "50–59", "Pilot_Count": 298, "AME_Count": 142, "Other_Count": 72, "Total": 512},
    {"Age_Bracket": "60+", "Pilot_Count": 80, "AME_Count": 44, "Other_Count": 31, "Total": 155},
]

PERSONNEL_ALERTS = [
    {"Callsign": "ATPL", "Flight_Level": "12 ATPL holders pending renewal — 5 already expired, risk of captain shortage on A350 fleet", "Risk_Score": 68},
    {"Callsign": "AME-B2", "Flight_Level": "Avionics licence renewal backlog — 11 pending, 4 expired, impacts line maintenance capacity", "Risk_Score": 72},
    {"Callsign": "AME-B1", "Flight_Level": "14 B1 renewals overdue; 3 suspended for lapsed training records", "Risk_Score": 65},
]

PERSONNEL_TACTICAL = [{
    "Tactical_Audit_ID": "TACT-PERS-2026-Q2",
    "Tail_ID": "CAAS PLSD — Personnel Licensing",
    "Composite_Risk_Score": 38,
    "Intelligence_Summary": "Personnel licensing overall healthy with 2,847 active licences. ATPL renewal backlog (12 pending, 5 already expired) is the top risk — potential A350 captain shortfall if not cleared within 60 days. AME-B2 avionics renewal backlog compounds the line-maintenance capacity pressure already flagged in the Q2 AMO audit. 60+ age bracket (80 pilots) represents succession risk for the A380 fleet.",
    "Action_1": "Flag all 5 lapsed ATPL holders to HR Crew Planning — assess A350 captain roster impact by next week.",
    "Action_2": "Joint AME-B2 renewal drive with SIA Engineering to clear 11 pending avionics licences before August 2026.",
    "Action_3": "Initiate 60+ pilot transition planning for A380 fleet — target 4 mentorship handovers in Q3.",
}]

PERSONNEL_RECENT = [
    {"Date": "2026-07-20", "Licence_Type": "ATPL", "Holder": "9V-SMA Captain", "Event": "Licence expired 2026-07-15 — renewal application incomplete, medical pending", "Status": "Urgent", "Summary": "Captain assigned to A350 fleet unable to operate until ATPL renewed; HR notified. Medical appointment scheduled 2026-07-28."},
    {"Date": "2026-07-18", "Licence_Type": "AME-B2", "Holder": "Avionics Technician S/N AV-2291", "Event": "Type rating renewal overdue — A320 avionics cert expired 2026-06-30", "Status": "In Progress", "Summary": "Technician removed from A320 line duties pending type-rating refresher. Training slot requested for 2026-08-02."},
    {"Date": "2026-07-14", "Licence_Type": "CPL", "Holder": "First Officer T. Nguyen", "Event": "Medical certificate suspended — hypertension requiring stabilisation", "Status": "Suspended", "Summary": "Class 1 medical suspended by CAAS Aeromedical Section. Fit-to-fly reassessment scheduled 2026-08-15. Assigned to desk duties in the interim."},
    {"Date": "2026-07-10", "Licence_Type": "AME-B1", "Holder": "Mechanical Technician S/N ME-1543", "Event": "Lapsed training records — 3 consecutive quarterly assessments not filed", "Status": "Suspended", "Summary": "Employment records show supervisor turnover gap. Remedial training plan submitted to CAAS PLSD."},
    {"Date": "2026-07-05", "Licence_Type": "Flight Dispatcher", "Holder": "Dispatcher K. Lim", "Event": "Recurrent training completed successfully — licence extended 12 months", "Status": "Renewed", "Summary": "All 234 dispatcher licences current. No further action required."},
]

# ============================================================================
# Domain 5 — Aircraft Registry (NEW)
# ============================================================================

AIRCRAFT_REGISTRY_OVERVIEW = [{
    "Category": "Singapore Aircraft Register",
    "Total_Registered": 416,
    "Active_Commercial": 182,
    "Active_Private": 76,
    "Active_Cargo": 38,
    "Government_Military": 42,
    "Under_Maintenance_Storage": 78,
    "Average_Fleet_Age_Years": 8.7,
    "Total_Seats_Commercial": 42850,
    "Reporting_Period": "2026-07-24",
}]

AIRCRAFT_FLEET = [
    {"Registration": "9V-SMA", "Aircraft_Type": "Airbus A350-900", "Operator": "Singapore Airlines", "Year_Manufactured": 2018, "Age_Years": 8, "Status": "Active", "Seats": 303, "Engine_Type": "RR Trent XWB", "Next_Due_Check": "2026-09-15"},
    {"Registration": "9V-SMB", "Aircraft_Type": "Airbus A350-900", "Operator": "Singapore Airlines", "Year_Manufactured": 2019, "Age_Years": 7, "Status": "Active", "Seats": 303, "Engine_Type": "RR Trent XWB", "Next_Due_Check": "2026-10-01"},
    {"Registration": "9V-SMC", "Aircraft_Type": "Airbus A350-900ULR", "Operator": "Singapore Airlines", "Year_Manufactured": 2020, "Age_Years": 6, "Status": "Active", "Seats": 253, "Engine_Type": "RR Trent XWB", "Next_Due_Check": "2026-08-20"},
    {"Registration": "9V-SHQ", "Aircraft_Type": "Airbus A380-800", "Operator": "Singapore Airlines", "Year_Manufactured": 2012, "Age_Years": 14, "Status": "Active", "Seats": 471, "Engine_Type": "RR Trent 900", "Next_Due_Check": "2026-11-05"},
    {"Registration": "9V-SHR", "Aircraft_Type": "Airbus A380-800", "Operator": "Singapore Airlines", "Year_Manufactured": 2013, "Age_Years": 13, "Status": "Active", "Seats": 441, "Engine_Type": "RR Trent 900", "Next_Due_Check": "2026-07-30"},
    {"Registration": "9V-SKQ", "Aircraft_Type": "Boeing 777-300ER", "Operator": "Singapore Airlines", "Year_Manufactured": 2015, "Age_Years": 11, "Status": "Active", "Seats": 278, "Engine_Type": "GE GE90-115B", "Next_Due_Check": "2026-09-10"},
    {"Registration": "9V-SKR", "Aircraft_Type": "Boeing 777-300ER", "Operator": "Singapore Airlines", "Year_Manufactured": 2016, "Age_Years": 10, "Status": "Active", "Seats": 278, "Engine_Type": "GE GE90-115B", "Next_Due_Check": "2026-08-05"},
    {"Registration": "9V-TTA", "Aircraft_Type": "Boeing 737-8 MAX", "Operator": "Scoot", "Year_Manufactured": 2022, "Age_Years": 4, "Status": "Active", "Seats": 178, "Engine_Type": "CFM LEAP-1B", "Next_Due_Check": "2027-01-20"},
    {"Registration": "9V-TTB", "Aircraft_Type": "Boeing 737-8 MAX", "Operator": "Scoot", "Year_Manufactured": 2023, "Age_Years": 3, "Status": "Active", "Seats": 178, "Engine_Type": "CFM LEAP-1B", "Next_Due_Check": "2027-03-15"},
    {"Registration": "9V-TCC", "Aircraft_Type": "Airbus A321neo", "Operator": "Scoot", "Year_Manufactured": 2021, "Age_Years": 5, "Status": "Active", "Seats": 236, "Engine_Type": "CFM LEAP-1A", "Next_Due_Check": "2026-12-01"},
    {"Registration": "9V-JSA", "Aircraft_Type": "Boeing 737-8 MAX", "Operator": "Jetstar Asia", "Year_Manufactured": 2019, "Age_Years": 7, "Status": "Active", "Seats": 180, "Engine_Type": "CFM LEAP-1B", "Next_Due_Check": "2026-10-10"},
    {"Registration": "9V-JSB", "Aircraft_Type": "Boeing 737-8 MAX", "Operator": "Jetstar Asia", "Year_Manufactured": 2020, "Age_Years": 6, "Status": "Active", "Seats": 180, "Engine_Type": "CFM LEAP-1B", "Next_Due_Check": "2026-10-15"},
    {"Registration": "9V-BGA", "Aircraft_Type": "Boeing 747-400F", "Operator": "Singapore Airlines Cargo", "Year_Manufactured": 2008, "Age_Years": 18, "Status": "Active", "Seats": 0, "Engine_Type": "PW PW4056", "Next_Due_Check": "2026-07-25"},
    {"Registration": "9V-BGB", "Aircraft_Type": "Boeing 777F", "Operator": "Singapore Airlines Cargo", "Year_Manufactured": 2017, "Age_Years": 9, "Status": "Active", "Seats": 0, "Engine_Type": "GE GE90-110B1", "Next_Due_Check": "2027-02-10"},
    {"Registration": "9V-SGE", "Aircraft_Type": "Airbus A350F", "Operator": "Singapore Airlines Cargo", "Year_Manufactured": 2024, "Age_Years": 2, "Status": "Active", "Seats": 0, "Engine_Type": "RR Trent XWB", "Next_Due_Check": "2028-06-01"},
    {"Registration": "9V-OTA", "Aircraft_Type": "Cessna 680 Citation Sovereign", "Operator": "Private — Orient Aviation", "Year_Manufactured": 2017, "Age_Years": 9, "Status": "Active", "Seats": 12, "Engine_Type": "PW PW306D", "Next_Due_Check": "2026-09-01"},
    {"Registration": "9V-PVA", "Aircraft_Type": "Gulfstream G650ER", "Operator": "Private — VistaJet", "Year_Manufactured": 2021, "Age_Years": 5, "Status": "Active", "Seats": 18, "Engine_Type": "RR BR725", "Next_Due_Check": "2027-05-15"},
]

AIRCRAFT_AGE_DISTRIBUTION = [
    {"Age_Bracket": "0–5 years", "Count": 108},
    {"Age_Bracket": "6–10 years", "Count": 142},
    {"Age_Bracket": "11–15 years", "Count": 86},
    {"Age_Bracket": "16–20 years", "Count": 52},
    {"Age_Bracket": "20+ years", "Count": 28},
]

AIRCRAFT_TYPE_BREAKDOWN = [
    {"Aircraft_Family": "Airbus A350", "Count": 58, "Avg_Age": 5.2},
    {"Aircraft_Family": "Airbus A380", "Count": 24, "Avg_Age": 13.5},
    {"Aircraft_Family": "Airbus A320/A321", "Count": 72, "Avg_Age": 6.8},
    {"Aircraft_Family": "Boeing 777", "Count": 34, "Avg_Age": 11.2},
    {"Aircraft_Family": "Boeing 737 NG/MAX", "Count": 68, "Avg_Age": 5.4},
    {"Aircraft_Family": "Boeing 747/777F (Cargo)", "Count": 38, "Avg_Age": 14.1},
    {"Aircraft_Family": "Business Jets / Turboprops", "Count": 76, "Avg_Age": 9.5},
    {"Aircraft_Family": "Helicopters", "Count": 46, "Avg_Age": 8.9},
]

AIRCRAFT_ALERTS = [
    {"Callsign": "9V-BGA", "Flight_Level": "B747-400F 18 years old — D-Check due July 2026. Potential grounding risk if not completed on schedule.", "Risk_Score": 82},
    {"Callsign": "9V-SHR", "Flight_Level": "A380 #2 approaching 14-year heavy maintenance visit. Spare parts lead time for Trent 900 may extend downtime.", "Risk_Score": 60},
    {"Callsign": "9V-SKQ", "Flight_Level": "777-300ER fleet average 10.5 years — GE90 performance restoration programme recommended for 3 airframes.", "Risk_Score": 48},
]

AIRCRAFT_TACTICAL = [{
    "Tactical_Audit_ID": "TACT-REG-2026-Q2",
    "Tail_ID": "CAAS ARB — Aircraft Registry",
    "Composite_Risk_Score": 44,
    "Intelligence_Summary": "Singapore aircraft register stands at 416 units with average fleet age of 8.7 years. 18-year-old B747-400F (9V-BGA) approaching critical D-Check — highest immediate risk. The A350 fleet (58 units, avg 5.2 years) is the youngest widebody fleet in the region. Scoot's 737-8 MAX fleet (avg 3.5 years) is well within maintenance maturity. 78 aircraft in storage/maintenance represent opportunity for reactivation planning.",
    "Action_1": "Track 9V-BGA D-Check milestone weekly — engage SIA Cargo on contingency freighter capacity if grounding extends beyond 14 days.",
    "Action_2": "Review 777-300ER GE90 performance data — recommend performance restoration for 3 high-cycle airframes by Q4 2026.",
    "Action_3": "Update registry records for 12 stored aircraft that may return to service for Changi T5 opening in 2026.",
}]

AIRCRAFT_RECENT = [
    {"Date": "2026-07-22", "Registration": "9V-SGE", "Operator": "SIA Cargo", "Event": "A350F entered service — first revenue flight SIN-NRT-SIN", "Type": "New Entry", "Status": "In Service", "Summary": "A350F (MSN 678) added to register 2026-07-20. First all-new cargo type for SIA Cargo since 777F."},
    {"Date": "2026-07-18", "Registration": "9V-TTB", "Operator": "Scoot", "Event": "737-8 MAX completed 2C check on schedule", "Type": "Maintenance", "Status": "Returned to Service", "Summary": "2C check completed without findings. Aircraft returned to service 2026-07-19."},
    {"Date": "2026-07-15", "Registration": "9V-PVA", "Operator": "VistaJet", "Event": "G650ER registration transferred from Malta to Singapore registry", "Type": "Registration Change", "Status": "Registered", "Summary": "Aircraft re-registered from 9H-VVA to 9V-PVA under Singapore registry. G650ER now SG-based."},
    {"Date": "2026-07-10", "Registration": "9V-SHR", "Operator": "Singapore Airlines", "Event": "A380 heavy maintenance check commenced", "Type": "Maintenance", "Status": "In Work", "Summary": "9V-SHR entered SIAEC hangar for scheduled 24-month heavy check. Estimated completion 2026-08-15."},
    {"Date": "2026-07-05", "Registration": "9V-BGA", "Operator": "SIA Cargo", "Event": "Pre-D-Check inspection identified 4 structural areas requiring NDT", "Type": "Inspection", "Status": "In Progress", "Summary": "B747-400F pre-D-Check found corrosion indicators on rear pressure bulkhead and wing rear spar. NDT scan scheduled."},
]

# ============================================================================
# Domain 6 — Aerodrome Incidents (NEW)
# ============================================================================

AERODROME_OVERVIEW = [{
    "Activity_Code": "Aerodrome Incident",
    "Primary_Aerodrome": "WSSS (Singapore Changi)",
    "Total_Incidents_YTD": 47,
    "Runway_Incursions": 8,
    "Ground_Handling_Incidents": 18,
    "FOD_Events": 12,
    "Wildlife_Strikes_On_Ground": 5,
    "Lighting_Navaid_Failures": 4,
    "Active_Investigations": 6,
    "Mean_Time_To_Resolve_Days": 14.3,
    "Reporting_Period": "2026 YTD (Jan–Jul)",
}]

AERODROME_INCIDENTS = [
    {"Track_ID": "AD-2026-001", "Callsign": "WSSS — RWY 02L", "Tail_ID": "Open", "Flight_Level": "Ground handling — GPU cable struck by pushback tractor", "Severity_Band": "medium", "Risk_Score": 42, "Occurrence_Date": "2026-07-14"},
    {"Track_ID": "AD-2026-002", "Callsign": "WSSS — Bay D41", "Tail_ID": "In Progress", "Flight_Level": "FOD found on taxiway — metal shard from tyre tread", "Severity_Band": "medium", "Risk_Score": 38, "Occurrence_Date": "2026-07-12"},
    {"Track_ID": "AD-2026-003", "Callsign": "WSSS — TWY F", "Tail_ID": "Closed", "Flight_Level": "Ground vehicle lost directional control on wet taxiway", "Severity_Band": "watch", "Risk_Score": 22, "Occurrence_Date": "2026-07-08"},
    {"Track_ID": "AD-2026-004", "Callsign": "WSSS — RWY 20R", "Tail_ID": "Open", "Flight_Level": "Approach lighting system — 3 inset lights inoperative", "Severity_Band": "high", "Risk_Score": 62, "Occurrence_Date": "2026-07-05"},
    {"Track_ID": "AD-2026-005", "Callsign": "WSSS — TWY G", "Tail_ID": "In Progress", "Flight_Level": "Fuel spill during refuelling — vehicle refueller hose disconnect", "Severity_Band": "high", "Risk_Score": 72, "Occurrence_Date": "2026-07-01"},
    {"Track_ID": "AD-2026-006", "Callsign": "WSSS — Bay C32", "Tail_ID": "Closed", "Flight_Level": "Baggage conveyor belt jam — damaged cargo container", "Severity_Band": "low", "Risk_Score": 12, "Occurrence_Date": "2026-06-28"},
    {"Track_ID": "AD-2026-007", "Callsign": "WSSS — RWY 02R", "Tail_ID": "Closed", "Flight_Level": "Bird strike on landing — no damage, FOD sweep conducted", "Severity_Band": "observation", "Risk_Score": 8, "Occurrence_Date": "2026-06-25"},
    {"Track_ID": "AD-2026-008", "Callsign": "WSSS — TWY A2", "Tail_ID": "Closed", "Flight_Level": "Standby power failure during lightning — EDG auto-start successful", "Severity_Band": "low", "Risk_Score": 15, "Occurrence_Date": "2026-06-22"},
]

AERODROME_HOTSPOTS = [
    {"Zone_ID": "GRID-AD-01", "Zone_Label": "TWY G — Refuelling bay cluster", "Location": "WSSS", "Center_Latitude": 1.3586, "Center_Longitude": 103.9888, "Event_Count": 7, "Severity_Band": "critical", "Loss_Alert_Count": 0},
    {"Zone_ID": "GRID-AD-02", "Zone_Label": "RWY 02L / 20R — Approach lighting corridor", "Location": "WSSS", "Center_Latitude": 1.3541, "Center_Longitude": 103.9871, "Event_Count": 5, "Severity_Band": "watch", "Loss_Alert_Count": 0},
    {"Zone_ID": "GRID-AD-03", "Zone_Label": "Bay D41–D45 — Ground handling zone", "Location": "WSSS", "Center_Latitude": 1.3468, "Center_Longitude": 103.9935, "Event_Count": 8, "Severity_Band": "critical", "Loss_Alert_Count": 1},
    {"Zone_ID": "GRID-AD-04", "Zone_Label": "TWY F — Wet-weather incident prone", "Location": "WSSS", "Center_Latitude": 1.3502, "Center_Longitude": 103.9921, "Event_Count": 4, "Severity_Band": "watch", "Loss_Alert_Count": 0},
]

AERODROME_ALERTS = [
    {"Callsign": "WSSS Approach Lighting", "Flight_Level": "RWY 20R inset lights 3 of 12 inoperative — low-vis capability degraded, NOTAM issued", "Risk_Score": 62},
    {"Callsign": "WSSS Refuelling Bay G", "Flight_Level": "Second fuel-spill incident in 30 days — hose disconnect pattern suggests training gap", "Risk_Score": 72},
    {"Callsign": "WSSS Bay C32", "Flight_Level": "Baggage handling incidents up 40% QoQ — investigation into conveyor maintenance schedule", "Risk_Score": 48},
]

AERODROME_TACTICAL = [{
    "Tactical_Audit_ID": "TACT-AD-2026-Q2",
    "Track_ID": "AD-2026-005",
    "Tail_ID": "WSSS Aerodrome Operations",
    "Composite_Risk_Score": 47,
    "Intelligence_Summary": "Aerodrome incident rate trending up 22% YoY driven by ground-handling (18 incidents) and FOD (12 incidents). Refuelling bay G accounts for 7 of 47 total incidents, including 2 fuel-spill events in 30 days — pattern suggests a procedure-compliance gap. RWY 20R approach lighting degradation reduces low-visibility capacity and requires urgent rectification. Bay D41–D45 ground handling zone is the highest-density incident location.",
    "Action_1": "Mandatory refuelling hose-connect refresher for all SATS ground handlers at WSSS by 2026-08-01 — 2 spill events in 30 days is a leading indicator.",
    "Action_2": "Expedite RWY 20R inset light replacement — engage Changi Airport Group (CAG) maintenance for 48-hour turnaround.",
    "Action_3": "Commission FOD-prevention walk on TWY A2–TWY F corridor within 7 days; review sweeper frequency for bay cluster D41–D45.",
}]

AERODROME_RECENT = [
    {"Date": "2026-07-20", "Location": "WSSS — TWY G", "Event": "Fuel spill during night refuelling — 40L Jet A1 released", "Type": "Fuel Spill", "Status": "Contained", "Summary": "Hose disconnect during A330 refuelling. Spill contained within 15 min. SATS handler stood down for retraining."},
    {"Date": "2026-07-16", "Location": "WSSS — RWY 20R", "Event": "Approach lighting degradation — 3 inset lights out", "Type": "Navaid Failure", "Status": "Awaiting Parts", "Summary": "Electrical fault in transformer for RWY 20R approach lighting. Spare transformer ordered from OEM lead time 14 days. NOTAM for CAT I minimums only."},
    {"Date": "2026-07-11", "Location": "WSSS — Bay D43", "Event": "GPU cable damaged by pushback tug", "Type": "Ground Handling", "Status": "Closed", "Summary": "GPU cable severed when pushback tug turned early. Cable replaced, tug driver counselled on marshalling signals."},
    {"Date": "2026-07-07", "Location": "WSSS — TWY F", "Event": "Service vehicle hydroplaned on wet taxiway", "Type": "Ground Vehicle", "Status": "Closed", "Summary": "Catering truck skidded on standing water during thunderstorm. No injuries, vehicle damage minor. TWY F drainage review initiated."},
    {"Date": "2026-07-03", "Location": "WSSS — TWY A2", "Event": "Metal shard found on taxiway — FOD sweep", "Type": "FOD", "Status": "Closed", "Summary": "Metal fragment (8cm x 2cm) from tyre tread found during scheduled sweep. All adjacent aircraft checked — no tyre damage found."},
]

# ============================================================================
# Domain 7 — Air Traffic Incidents (NEW)
# ============================================================================

ATC_OVERVIEW = [{
    "Activity_Code": "Air Traffic Incident",
    "Primary_ACC": "SIN ARR / DEP (Singapore FIR)",
    "Total_Incidents_YTD": 31,
    "Loss_Separation_Events": 5,
    "Communication_Failures": 8,
    "ATC_Coordination_Errors": 6,
    "Radio_Frequency_Occlusion": 7,
    "CNS_Failures": 5,
    "Operational_Errors": 4,
    "Pilot_Deviation_Events": 7,
    "Active_Investigations": 4,
    "Reporting_Period": "2026 YTD (Jan–Jul)",
}]

ATC_INCIDENTS = [
    {"Track_ID": "ATC-2026-001", "Callsign": "SIN ACC Sector 5", "Tail_ID": "In Progress", "Flight_Level": "Loss of separation — 3 NM / 700 ft between SIA12 and TGW47", "Severity_Band": "critical", "Risk_Score": 88, "Occurrence_Date": "2026-07-15"},
    {"Track_ID": "ATC-2026-002", "Callsign": "SIN Departure", "Tail_ID": "Closed", "Flight_Level": "Radio frequency occlusion — TWR freq stepped on by ground maintenance radio", "Severity_Band": "high", "Risk_Score": 64, "Occurrence_Date": "2026-07-11"},
    {"Track_ID": "ATC-2026-003", "Callsign": "SIN ACC Sector 2", "Tail_ID": "Closed", "Flight_Level": "Coordination error — handoff to Ho Chi Minh ACC delayed by 8 minutes", "Severity_Band": "medium", "Risk_Score": 52, "Occurrence_Date": "2026-07-08"},
    {"Track_ID": "ATC-2026-004", "Callsign": "SIN Approach", "Tail_ID": "Open", "Flight_Level": "CNS failure — secondary radar (MSSR) outage for 14 minutes", "Severity_Band": "high", "Risk_Score": 74, "Occurrence_Date": "2026-07-04"},
    {"Track_ID": "ATC-2026-005", "Callsign": "SIN TWR", "Tail_ID": "Closed", "Flight_Level": "Pilot deviation — aircraft climbed above assigned altitude on departure", "Severity_Band": "medium", "Risk_Score": 45, "Occurrence_Date": "2026-07-01"},
    {"Track_ID": "ATC-2026-006", "Callsign": "SIN ACC Sector 4", "Tail_ID": "Closed", "Flight_Level": "Loss of separation — 4.5 NM horizontal between two B777s converging on same waypoint", "Severity_Band": "high", "Risk_Score": 68, "Occurrence_Date": "2026-06-27"},
    {"Track_ID": "ATC-2026-007", "Callsign": "SIN ATIS", "Tail_ID": "Closed", "Flight_Level": "ATIS broadcast corrupted — digital voice synthesiser glitch for 3 cycles", "Severity_Band": "low", "Risk_Score": 18, "Occurrence_Date": "2026-06-22"},
    {"Track_ID": "ATC-2026-008", "Callsign": "SIN ACC Sector 1", "Tail_ID": "In Progress", "Flight_Level": "Communication failure — VHF Guard 121.5 receiver intermittent for 6 hours", "Severity_Band": "high", "Risk_Score": 70, "Occurrence_Date": "2026-06-18"},
]

ATC_HOTSPOTS = [
    {"Zone_ID": "GRID-ATC-01", "Zone_Label": "Sector 5 — South-bound traffic convergence", "Location": "SIN FIR", "Center_Latitude": 1.3240, "Center_Longitude": 104.0120, "Event_Count": 6, "Severity_Band": "critical", "Loss_Alert_Count": 2},
    {"Zone_ID": "GRID-ATC-02", "Zone_Label": "Sector 2 — Handoff boundary to HCM ACC", "Location": "SIN FIR", "Center_Latitude": 1.4180, "Center_Longitude": 104.5890, "Event_Count": 4, "Severity_Band": "watch", "Loss_Alert_Count": 0},
    {"Zone_ID": "GRID-ATC-03", "Zone_Label": "SIN APP — Arrival stack alignment", "Location": "SIN FIR", "Center_Latitude": 1.3400, "Center_Longitude": 103.9500, "Event_Count": 5, "Severity_Band": "critical", "Loss_Alert_Count": 1},
    {"Zone_ID": "GRID-ATC-04", "Zone_Label": "Sector 3 — Over-flying transit corridor", "Location": "SIN FIR", "Center_Latitude": 1.2900, "Center_Longitude": 104.2300, "Event_Count": 3, "Severity_Band": "watch", "Loss_Alert_Count": 0},
]

ATC_ALERTS = [
    {"Callsign": "SIN ACC Sector 5", "Flight_Level": "2 loss-separation events in Sector 5 within 30 days — traffic complexity exceeds sector capacity threshold", "Risk_Score": 88},
    {"Callsign": "SIN Approach", "Flight_Level": "MSSR secondary radar outage on 2026-07-04 — backup system activation revealed training gaps for 3 controllers", "Risk_Score": 74},
    {"Callsign": "SIN ACC Sector 1", "Flight_Level": "VHF Guard 121.5 intermittent — primary backup receiver not fail-safe. Single-point-of-failure risk identified", "Risk_Score": 70},
    {"Callsign": "SIN Departure", "Flight_Level": "Radio occlusion from ground maintenance on TWR freq — maintenance crew coordination procedure needs revision", "Risk_Score": 64},
]

ATC_TACTICAL = [{
    "Tactical_Audit_ID": "TACT-ATC-2026-Q2",
    "Track_ID": "ATC-2026-001",
    "Tail_ID": "CAAS ANS — Air Traffic Services",
    "Composite_Risk_Score": 62,
    "Intelligence_Summary": "SIN FIR air traffic incidents trending up 18% YoY. Sector 5 is the primary concern with 2 loss-separation events in 30 days and traffic complexity exceeding rated sector capacity during peak hours. The MSSR secondary radar outage in July exposed procedural gaps in backup-system handover — 3 controllers required refresher training. VHF Guard 121.5 receiver intermittency creates a single-point-of-failure risk for emergency comms. The HCM ACC handoff delay (8 minutes) indicates coordination-letter procedures may need review.",
    "Action_1": "Commission immediate sector capacity review for SIN ACC Sector 5 — 2 loss-separation events in 30 days requires either flow-restriction or sector-splitting by Q3 2026.",
    "Action_2": "Schedule MSSR backup-procedure simulator training for all Approach controllers by 2026-08-15.",
    "Action_3": "Reassess VHF Guard 121.5 redundancy architecture with CNS engineering — initiate procurement for secondary backup receiver.",
}]

ATC_RECENT = [
    {"Date": "2026-07-18", "Location": "SIN ACC Sector 5", "Event": "Post-incident analysis for loss-separation (3NM/700ft) complete", "Type": "Investigation", "Status": "Open", "Summary": "Loss of separation between SIA12 (A350, FL350) and TGW47 (B788, FL343) on 2026-07-15. Cause: controller workload — 18 aircraft under active control during sector peak. Recommendation: sector-splitting or flow-rate cap."},
    {"Date": "2026-07-14", "Location": "SIN APP", "Event": "MSSR radar restored — root cause identified as power supply module failure", "Type": "CNS Restoration", "Status": "Closed", "Summary": "MSSR secondary radar (Model: Raytheon Condor Mk3) power supply module failure caused 14-min outage. Module replaced 2026-07-13. Backup-procedure training initiated."},
    {"Date": "2026-07-10", "Location": "SIN TWR", "Event": "Frequency occlusion incident closed — root cause: maintenance radio cross-coupling", "Type": "Frequency Incident", "Status": "Closed", "Summary": "Ground maintenance VHF radio inadvertently transmitting on Tower freq 118.2 for 4 minutes. Maintenance radio procedure revised; ground crew briefed."},
    {"Date": "2026-07-06", "Location": "SIN ACC Sector 1", "Event": "VHF Guard 121.5 intermittent fault traced to antenna cable moisture ingress", "Type": "CNS Fault", "Status": "Awaiting Repair", "Summary": "Moisture ingress at J7 connector on antenna cable run. Temporary fix applied; permanent cable replacement scheduled for next maintenance window (2026-08-02)."},
    {"Date": "2026-07-02", "Location": "SIN ACC Sector 2", "Event": "Coordination delay with HCM ACC — corrective action plan agreed", "Type": "Coordination", "Status": "Closed", "Summary": "Handoff delay caused by controller not following amended letter of agreement (LoA) for flight level transfer. LoA re-briefed to all Sector 2 controllers."},
]


# ============================================================================
# Demo dispatcher
# ============================================================================

async def run_mock_chat_stream(session_id: str, message: str) -> AsyncIterator[dict]:
    del session_id
    text = message.strip().lower()

    # Match specific domains
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

    # === NEW DOMAINS ===
    if "personnel" in text or "licence" in text or "pilot" in text or "ame" in text or "engineer" in text:
        async for event in _personnel_dashboard_events():
            yield event
        return

    if "aircraft" in text or "registry" in text or "register" in text or "fleet" in text or "9V-" in message:
        async for event in _aircraft_registry_dashboard_events():
            yield event
        return

    if "aerodrome" in text or "airfield" in text or "changi" in text or "wsss" in text or ("ground" in text and "incident" in text):
        async for event in _aerodrome_dashboard_events():
            yield event
        return

    if "atc" in text or "air traffic" in text or "loss of separation" in text or "sector" in text:
        async for event in _atc_dashboard_events():
            yield event
        return

    # Cross-domain aggregation: comprehensive safety overview
    if "overview" in text or "summary" in text or "all" in text or "cross" in text or "aggregate" in text:
        async for event in _cross_domain_overview_events():
            yield event
        return

    # Catch specific queries on metrics
    if "active pilot" in text or "how many pilot" in text:
        async for event in _personnel_dashboard_events():
            yield event
        return

    yield {
        "type": "final",
        "data": "Demo mode is active. Try one of these prompts: 'Show me the runway incursion dashboard', 'Analyze recent bird strike', 'Audit aircraft maintenance organisation', 'Show personnel licences summary', 'Aircraft registry overview', 'Aerodrome incidents at Changi', 'ATC incident analysis', 'Cross-domain safety overview'.",
    }


# ============================================================================
# Existing dashboard event generators
# ============================================================================

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


# ============================================================================
# NEW — Personnel Licences Dashboard
# ============================================================================

async def _personnel_dashboard_events() -> AsyncIterator[dict]:
    expiry_chart = chart_spec(PERSONNEL_EXPIRY_TREND, intent="line", x="Month", y="Expiring_Licences", title="Monthly licence expiry trend")
    age_chart = chart_spec(PERSONNEL_AGE_DEMOGRAPHIC, intent="bar", x="Age_Bracket", y="Total", title="Licence holder age demographics")
    dashboard = dashboard_spec(
        datasets=[
            {"name": "overview", "rows": PERSONNEL_OVERVIEW},
            {"name": "personnel_licences", "rows": PERSONNEL_LICENCES},
            {"name": "personnel_expiry_trend", "rows": PERSONNEL_EXPIRY_TREND},
            {"name": "personnel_age_demographic", "rows": PERSONNEL_AGE_DEMOGRAPHIC},
            {"name": "alerts", "rows": PERSONNEL_ALERTS},
            {"name": "tactical_audit", "rows": PERSONNEL_TACTICAL},
            {"name": "recent_records", "rows": PERSONNEL_RECENT},
        ],
        title="Personnel Licensing Dashboard",
        domain="personnel_licences",
        focus="CAAS PLSD — Licence holder status and renewal tracking",
    )
    for name, output in (
        ("chart_spec", expiry_chart),
        ("chart_spec", age_chart),
        ("dashboard_spec", dashboard),
    ):
        yield {"type": "tool_call", "data": {"name": name, "arguments": {"demo": True}}}
        await asyncio.sleep(0)
        yield {"type": "tool_result", "data": {"name": name, "output": json.dumps(output)}}
        await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "- 2,847 active licences total: 1,634 pilots (57%), 852 AMEs (30%), 127 flight engineers, 234 flight dispatchers.\n- ATPL renewal backlog is the top risk — 12 pending, 5 already expired. Potential A350 captain shortfall.\n- 89 licences expiring within 90 days across all categories — renewal surge expected in Q3.\n- 60+ age bracket has 80 pilots — A380 fleet succession planning recommended.\n- AME-B2 avionics renewal backlog (11 pending) compounds SIA Engineering line-maintenance capacity pressure.\n\nSources:\n- vw_SafetyIntel_PersonnelLicences\n- vw_SafetyIntel_PersonnelExpiryTrend\n- vw_SafetyIntel_PersonnelDemographics",
    }


# ============================================================================
# NEW — Aircraft Registry Dashboard
# ============================================================================

async def _aircraft_registry_dashboard_events() -> AsyncIterator[dict]:
    age_chart = chart_spec(AIRCRAFT_AGE_DISTRIBUTION, intent="bar", x="Age_Bracket", y="Count", title="Aircraft age distribution")
    type_chart = chart_spec(AIRCRAFT_TYPE_BREAKDOWN, intent="bar", x="Aircraft_Family", y="Count", title="Fleet composition by type")
    dashboard = dashboard_spec(
        datasets=[
            {"name": "overview", "rows": AIRCRAFT_REGISTRY_OVERVIEW},
            {"name": "aircraft_fleet", "rows": AIRCRAFT_FLEET},
            {"name": "aircraft_age_distribution", "rows": AIRCRAFT_AGE_DISTRIBUTION},
            {"name": "aircraft_type_breakdown", "rows": AIRCRAFT_TYPE_BREAKDOWN},
            {"name": "alerts", "rows": AIRCRAFT_ALERTS},
            {"name": "tactical_audit", "rows": AIRCRAFT_TACTICAL},
            {"name": "recent_records", "rows": AIRCRAFT_RECENT},
        ],
        title="Aircraft Registry Dashboard",
        domain="aircraft_registry",
        focus="Singapore Aircraft Register — fleet composition and maintenance status",
    )
    for name, output in (
        ("chart_spec", age_chart),
        ("chart_spec", type_chart),
        ("dashboard_spec", dashboard),
    ):
        yield {"type": "tool_call", "data": {"name": name, "arguments": {"demo": True}}}
        await asyncio.sleep(0)
        yield {"type": "tool_result", "data": {"name": name, "output": json.dumps(output)}}
        await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "- 416 aircraft on the Singapore Register: 182 active commercial, 76 private, 38 cargo, 42 government/military, 78 in storage/maintenance.\n- Average fleet age is 8.7 years. A350 fleet (58 units, avg 5.2 years) is the youngest widebody fleet in the region.\n- Highest risk: 9V-BGA (B747-400F, 18 years) approaching critical D-Check — potential grounding if not completed on schedule.\n- Scoot's 737-8 MAX fleet (avg 3.5 years) well within maintenance maturity.\n- New A350F cargo aircraft (9V-SGE) entered service 2026-07-20 — first all-new cargo type for SIA Cargo.\n\nSources:\n- vw_SafetyIntel_AircraftRegistry\n- vw_SafetyIntel_AircraftAgeDistribution\n- vw_SafetyIntel_FleetComposition",
    }


# ============================================================================
# NEW — Aerodrome Incidents Dashboard
# ============================================================================

async def _aerodrome_dashboard_events() -> AsyncIterator[dict]:
    dashboard = dashboard_spec(
        datasets=[
            {"name": "overview", "rows": AERODROME_OVERVIEW},
            {"name": "tracks", "rows": AERODROME_INCIDENTS},
            {"name": "hotspots", "rows": AERODROME_HOTSPOTS},
            {"name": "alerts", "rows": AERODROME_ALERTS},
            {"name": "tactical_audit", "rows": AERODROME_TACTICAL},
            {"name": "recent_records", "rows": AERODROME_RECENT},
        ],
        title="Aerodrome Incident Dashboard",
        domain="aerodrome_incidents",
        focus="WSSS Changi Aerodrome — ground handling, FOD, lighting, and fuel-spill monitoring",
    )
    yield {"type": "tool_call", "data": {"name": "dashboard_spec", "arguments": {"domain": "aerodrome_incidents"}}}
    await asyncio.sleep(0)
    yield {"type": "tool_result", "data": {"name": "dashboard_spec", "output": json.dumps(dashboard)}}
    await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "- 47 aerodrome incidents YTD at WSSS — up 22% YoY. Ground handling (18) and FOD (12) dominate.\n- Refuelling Bay G is the highest-risk zone — 2 fuel-spill events in 30 days suggest a procedure-compliance gap.\n- RWY 20R approach lighting degradation (3 of 12 inset lights inoperative) reduces low-visibility capacity. NOTAM issued.\n- Bay D41–D45 ground handling zone has highest incident density (8 events) — conveyor, GPU, and pushback-related.\n- Mean time to resolve: 14.3 days. 6 active investigations.\n\nSources:\n- vw_SafetyIntel_AerodromeIncidents\n- vw_SafetyIntel_AerodromeHotspots\n- vw_SafetyIntel_Occurrences",
    }


# ============================================================================
# NEW — Air Traffic Incidents Dashboard
# ============================================================================

async def _atc_dashboard_events() -> AsyncIterator[dict]:
    dashboard = dashboard_spec(
        datasets=[
            {"name": "overview", "rows": ATC_OVERVIEW},
            {"name": "tracks", "rows": ATC_INCIDENTS},
            {"name": "hotspots", "rows": ATC_HOTSPOTS},
            {"name": "alerts", "rows": ATC_ALERTS},
            {"name": "tactical_audit", "rows": ATC_TACTICAL},
            {"name": "recent_records", "rows": ATC_RECENT},
        ],
        title="Air Traffic Incident Dashboard",
        domain="atc_incidents",
        focus="SIN FIR — loss of separation, CNS failures, and coordination gaps",
    )
    yield {"type": "tool_call", "data": {"name": "dashboard_spec", "arguments": {"domain": "atc_incidents"}}}
    await asyncio.sleep(0)
    yield {"type": "tool_result", "data": {"name": "dashboard_spec", "output": json.dumps(dashboard)}}
    await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "- 31 air traffic incidents YTD in SIN FIR — up 18% YoY. Loss of separation (5) and communication failures (8) are top categories.\n- Sector 5 is the primary concern — 2 loss-separation events in 30 days. Traffic complexity exceeds rated sector capacity.\n- MSSR secondary radar outage (14 min) revealed procedural gaps in backup-system handover for 3 Approach controllers.\n- VHF Guard 121.5 receiver intermittency creates a single-point-of-failure risk for emergency communications.\n- Coordination delay with Ho Chi Minh ACC (8 min) led to LoA re-briefing for all Sector 2 controllers.\n\nSources:\n- vw_SafetyIntel_ATCIncidents\n- vw_SafetyIntel_ATCTrafficHotspots\n- vw_SafetyIntel_Occurrences",
    }


# ============================================================================
# NEW — Cross-domain safety overview (aggregation across data sources)
# ============================================================================

async def _cross_domain_overview_events() -> AsyncIterator[dict]:
    """Aggregate insights from ALL data sources into one cross-domain view."""
    yield {
        "type": "tool_result",
        "data": {
            "name": "dashboard_spec",
            "output": json.dumps({
                "type": "ops_dashboard",
                "title": "Cross-Domain Safety Intelligence Overview",
                "domain": "cross_domain",
                "focus": "Aggregated safety picture across all CAAS domains",
                "generated_at": "2026-07-24T05:33:00Z",
                "metrics": [
                    {"label": "Domains", "value": "7", "detail": "monitored domains"},
                    {"label": "Total incidents YTD", "value": "78", "detail": "aerodrome + ATC + bird + runway"},
                    {"label": "Active licences", "value": "2,847", "detail": "pilots + AMEs + dispatchers"},
                    {"label": "Aircraft registered", "value": "416", "detail": "Singapore register"},
                    {"label": "Open investigations", "value": "10", "detail": "across all domains"},
                ],
                "hotspots": [],
                "tracks": [],
                "alerts": [
                    {"callsign": "Sector 5 — Loss Separation", "flight_level": "2 events in 30 days — sector capacity review required", "risk_score": 88},
                    {"callsign": "9V-BGA D-Check", "flight_level": "B747-400F 18yr old approaching critical maintenance", "risk_score": 82},
                    {"callsign": "ATPL Renewal Backlog", "flight_level": "12 pending, 5 expired — captain shortfall risk", "risk_score": 68},
                    {"callsign": "Refuelling Bay G", "flight_level": "2 fuel-spill events in 30 days — training gap", "risk_score": 72},
                ],
                "tactical_audit": {
                    "tail_id": "CAAS SRD — Cross-Domain",
                    "composite_risk_score": 55,
                    "summary": "Cross-domain analysis (Q2 2026) identifies Sector 5 ATC capacity, 9V-BGA D-Check, and ATPL renewal backlog as the three highest cross-domain risks. Personnel licensing (38) and aircraft registry (44) have manageable risk profiles but contribute to downstream maintenance and crewing constraints. Aerodrome incidents (47 YTD, up 22% YoY) and ATC incidents (31 YTD, up 18% YoY) both show upward trends that warrant cross-functional safety action.",
                    "actions": [
                        "Sector 5 sector-splitting or flow-rate cap — cross-reference with crew licensing to ensure sufficient rated controllers.",
                        "9V-BGA D-Check contingency planning — coordinate with registry and maintenance scheduling teams.",
                        "ATPL renewal surge (89 expiring within 90 days) — coordinate with airline HR and CAAS PLSD.",
                    ],
                },
                "recent_records": [
                    {"Domain": "Aerodrome", "Date": "2026-07-20", "Location": "WSSS TWY G", "Event": "Fuel spill — 40L Jet A1", "Status": "Contained"},
                    {"Domain": "ATC", "Date": "2026-07-18", "Location": "Sector 5", "Event": "Loss separation analysis complete", "Status": "Open"},
                    {"Domain": "Aircraft Registry", "Date": "2026-07-22", "Location": "SIA Cargo", "Event": "A350F entered service", "Status": "In Service"},
                    {"Domain": "Personnel", "Date": "2026-07-20", "Location": "ATPL", "Event": "Captain licence expired", "Status": "Urgent"},
                    {"Domain": "Runway", "Date": "2025-01-22", "Location": "WSSS", "Event": "Unauthorised entry", "Status": "In-progress"},
                ],
                "highlights": [
                    "7 domains monitored: Runway Incursion, Bird Strike, AMO Audit, Personnel Licences, Aircraft Registry, Aerodrome Incidents, ATC Incidents.",
                    "78 total incidents YTD across aerodrome and ATC domains — combined trend up 20% YoY.",
                    "10 open investigations across all domains — Sector 5 and 9V-BGA are the highest-rated individual risks.",
                    "ATPL renewal backlog (12 pending, 5 expired) flagged as cross-domain crewing risk for airline operations.",
                ],
                "datasets": [
                    {"name": "cross_domain_metrics", "title": "Cross-Domain Metrics", "row_count": 7},
                ],
            }),
        },
    }
    await asyncio.sleep(0)
    yield {
        "type": "final",
        "data": "## Cross-Domain Safety Intelligence Overview\n\n**7 domains** are being actively monitored. Here's the aggregated picture:\n\n### Key metrics\n- **2,847** active licences · **416** aircraft registered · **78** total incidents YTD\n- **10** open investigations across all domains\n- Total incident trend: **up ~20% YoY** across aerodrome and ATC domains\n\n### Top cross-domain risks (Top 3)\n1. **Sector 5** (ATC) — 2 loss-separation events in 30 days. Sector capacity review recommended.\n2. **9V-BGA** (Aircraft Registry) — B747-400F D-Check critical milestone. Fleet capacity at risk if grounding extended.\n3. **ATPL Renewal Backlog** (Personnel) — 12 pending, 5 expired. Potential A350 captain shortfall.\n\n### Emerging patterns\n- Aerodrome incidents (47 YTD, +22% YoY) and ATC incidents (31 YTD, +18% YoY) both trending up.\n- AME-B2 avionics renewal backlog compounds SIA Engineering's maintenance capacity pressure (see AMO audit).\n- A350F (9V-SGE) entered service 2026-07-20 — positive fleet modernisation signal.\n\n### Recommended cross-functional actions\n1. Sector 5: coordinate between ATS and Personnel Licensing for rated controller availability.\n2. 9V-BGA: cross-reference D-Check timeline with SIA Cargo fleet plan and registry records.\n3. ATPL renewal: coordinate processing surge with airline HR and CAAS PLSD. \\n\\nSources: vw_SafetyIntel overview of all 7 domains.",
    }
