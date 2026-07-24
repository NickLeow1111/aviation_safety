/***************************************************************************************
 Script  : Occurrence-Ops PoC views for the Safety Intelligence Bot (GOLD schema)
 Target  : Azure Synapse Analytics - Dedicated SQL Pool
 Purpose : Views section of rollout_occurrence_ops_poc.sql, re-pointed at the
           "gold" schema. Base tables (DM_TBL_SRG_OCCURRENCE_OPS_TRACK,
           DM_TBL_SRG_OCCURRENCE_HOTSPOT, DM_TBL_SRG_TACTICAL_AUDIT) are assumed
           to already exist and be loaded in the "gold" schema.

 NOTE: This file contains the VIEWS ONLY. The table DDL + synthetic INSERTs from
       rollout_occurrence_ops_poc.sql are intentionally omitted, since the base
       tables already exist in gold in the customer environment.

 See the AGENT-SIDE CHANGES note at the bottom of vw_SafetyIntel_Views_Gold.sql.
****************************************************************************************/

------------------------------------------------------------------------------
-- Ensure the gold schema exists (CREATE SCHEMA must be its own batch)
------------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'gold')
    EXEC('CREATE SCHEMA gold');
GO

/* -------------------------------------------------------------------------
   Views exposed to the NL2SQL agent
   ------------------------------------------------------------------------- */

IF OBJECT_ID('gold.vw_SafetyIntel_OccurrenceOpsOverview','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_OccurrenceOpsOverview;
GO
CREATE VIEW gold.vw_SafetyIntel_OccurrenceOpsOverview AS
SELECT
    t.Activity_Code,
    COUNT(*) AS Active_Tracks,
    CAST(AVG(t.Jamming_Index_Pct) AS DECIMAL(5,2)) AS Jamming_Index_Pct,
    CAST(AVG(t.Integrity_Index_Pct) AS DECIMAL(5,2)) AS Integrity_Index_Pct,
    SUM(CASE WHEN t.Conflict_Alert IS NOT NULL THEN 1 ELSE 0 END) AS Loss_Alert_Count,
    COUNT(*) AS Records_Analyzed,
    MAX(t.Location) AS Primary_Location
FROM gold.DM_TBL_SRG_OCCURRENCE_OPS_TRACK t
GROUP BY t.Activity_Code;
GO

IF OBJECT_ID('gold.vw_SafetyIntel_OccurrenceOps','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_OccurrenceOps;
GO
CREATE VIEW gold.vw_SafetyIntel_OccurrenceOps AS
SELECT
    t.Track_ID,
    t.Snapshot_Timestamp,
    t.Activity_Code,
    t.Approval_Number AS AWI,
    t.Organisation_Name,
    t.Callsign,
    t.Tail_ID,
    t.Location,
    t.Latitude,
    t.Longitude,
    t.Heading_Deg,
    t.Ground_Speed_Kts,
    t.Altitude_Ft,
    t.Flight_Level,
    t.Integrity_Index_Pct,
    t.Jamming_Index_Pct,
    t.Conflict_Risk_Score,
    t.Conflict_Alert,
    t.Conflict_Pair,
    t.Current_Status
FROM gold.DM_TBL_SRG_OCCURRENCE_OPS_TRACK t;
GO

IF OBJECT_ID('gold.vw_SafetyIntel_OccurrenceHotspots','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_OccurrenceHotspots;
GO
CREATE VIEW gold.vw_SafetyIntel_OccurrenceHotspots AS
SELECT
    h.Zone_ID,
    h.Activity_Code,
    h.Location,
    h.Zone_Label,
    h.Center_Latitude,
    h.Center_Longitude,
    h.Event_Count,
    h.Severity_Band,
    h.Loss_Alert_Count,
    h.Integrity_Index_Pct,
    h.Jamming_Index_Pct
FROM gold.DM_TBL_SRG_OCCURRENCE_HOTSPOT h;
GO

IF OBJECT_ID('gold.vw_SafetyIntel_TacticalAudit','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_TacticalAudit;
GO
CREATE VIEW gold.vw_SafetyIntel_TacticalAudit AS
SELECT
    a.Tactical_Audit_ID,
    a.Activity_Code,
    a.Track_ID,
    a.Tail_ID,
    a.Composite_Risk_Score,
    a.Intelligence_Summary,
    a.Action_1,
    a.Action_2,
    a.Action_3
FROM gold.DM_TBL_SRG_TACTICAL_AUDIT a;
GO
