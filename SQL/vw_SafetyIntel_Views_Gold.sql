/***************************************************************************************
 Script  : Synapse views for the Safety Intelligence Bot (GOLD schema)
 Target  : Azure Synapse Analytics - Dedicated SQL Pool
 Purpose : Friendly, pre-joined views consumed by the NL2SQL agent. Names are
           "vw_SafetyIntel_*" so the LLM only sees a small, well-shaped surface.

 GOLD variant:
   - Identical to vw_SafetyIntel_Views.sql but every object (view + base table)
     lives in the "gold" schema instead of "dbo".
   - Use this when the customer landed the DM_TBL_SRG_* base tables in "gold".
   - Agent side must also point at gold (see notes at the bottom of this file).

 Notes:
   - All views are read-only logical views. The agent's SQL allow-list permits
     SELECT only on objects whose name starts with "vw_SafetyIntel_".
   - Synapse does not enforce FKs; joins are by AWI / Approval_Number.
   - Keep grain in the comment header for each view so it is easy for the LLM
     to reason about COUNT/GROUP BY queries.
****************************************************************************************/

------------------------------------------------------------------------------
-- Ensure the gold schema exists (CREATE SCHEMA must be its own batch)
------------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'gold')
    EXEC('CREATE SCHEMA gold');
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_AMO
-- Grain: one row per AMO (AWI). Adds latest tier and assigned PMI.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_AMO','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_AMO;
GO
CREATE VIEW gold.vw_SafetyIntel_AMO AS
SELECT
    m.Approval_Number                        AS AWI,
    m.Name_of_AMO                            AS Organisation_Name,
    m.Location,
    m.Country,
    m.City,
    m.Highest_Rating,
    m.[Status],
    m.Bilateral_Arrangement,
    m.Initial_Issue_Date,
    m.Approval_From,
    m.Approval_To,
    m.AM_Name                                AS Accountable_Manager,
    m.QM_Name                                AS Quality_Manager,
    ly.Latest_Year                           AS Tier_Year,
    t.Tier                                   AS Current_Tier,
    p.PMI                                    AS Assigned_PMI
FROM gold.DM_TBL_SRG_AMO_MOA m
LEFT JOIN (
    SELECT AWI, MAX([Year]) AS Latest_Year
    FROM gold.DM_TBL_SRG_AMO_TIER
    GROUP BY AWI
) ly ON ly.AWI = m.Approval_Number
LEFT JOIN gold.DM_TBL_SRG_AMO_TIER t
    ON t.AWI = ly.AWI AND t.[Year] = ly.Latest_Year
LEFT JOIN gold.DM_TBL_SRG_AMO_ASSIGNED_PMI p
    ON p.AWI_No = m.Approval_Number;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_Findings
-- Grain: one row per CAN/OBS/DIS finding raised against an AMO.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_Findings','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_Findings;
GO
CREATE VIEW gold.vw_SafetyIntel_Findings AS
SELECT
    f.Approval_Number                        AS AWI,
    f.Organisation_Name,
    f.Evaluation_Action_Type                 AS Finding_Type, -- CAN/OBS/DIS
    f.Evaluation_Action_Issue_Date           AS Finding_Date,
    YEAR(f.Evaluation_Action_Issue_Date)     AS Finding_Year,
    f.Area_Audited,
    f.Level_Of_Finding,
    f.Critical_Element,
    -- Normalise root cause into Technical / Process / Human (used by Root Cause Attribution chart)
    CASE
        WHEN f.Root_Cause_Classification IN ('Human Factors','Training/Competency') THEN 'Human'
        WHEN f.Root_Cause_Classification IN ('Process/Procedure gap','Document control','Miscommunication/Lack of communication','Closure related','Organisational') THEN 'Process'
        WHEN f.Root_Cause_Classification IN ('Resource/Staffing','Facilities/Environment','Safety/HSE') THEN 'Technical'
        ELSE 'Process'
    END                                      AS Root_Cause_Bucket,
    f.Root_Cause_Classification              AS Root_Cause_Detail,
    f.Non_Compliance_Statement,
    f.Immediate_Action,
    f.Follow_Up_And_Closure
FROM gold.DM_TBL_SRG_AMO_CAN_OBS_DIS_SUMMARY f;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_Audits
-- Grain: one row per audit instance (planned or completed).
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_Audits','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_Audits;
GO
CREATE VIEW gold.vw_SafetyIntel_Audits AS
SELECT
    a.Approval_No                            AS AWI,
    a.Organisations                          AS Organisation_Name,
    a.Audit_Type,
    a.CAT,
    a.Country,
    a.City,
    a.Approval_Expiry_Date,
    a.Audit_Plan                             AS Planned_Audit_Date,
    a.Completed_Audit_Date,
    a.Previous_Year_PMI,
    a.Current_Year_PMI,
    a.ESOMS_AMO_Audit_Reference_No           AS ESOMS_Reference_No,
    a.Remarks
FROM gold.DM_TBL_SRG_AMO_AUDIT_TRACKING a;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_TierTrend
-- Grain: AWI x Year. Useful for tier-change queries.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_TierTrend','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_TierTrend;
GO
CREATE VIEW gold.vw_SafetyIntel_TierTrend AS
SELECT
    t.AWI,
    m.Name_of_AMO                            AS Organisation_Name,
    t.[Year],
    t.Tier,
    t.Highest_Rating
FROM gold.DM_TBL_SRG_AMO_TIER t
LEFT JOIN gold.DM_TBL_SRG_AMO_MOA m ON m.Approval_Number = t.AWI;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_Surveillance
-- Grain: one row per surveillance activity.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_Surveillance','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_Surveillance;
GO
CREATE VIEW gold.vw_SafetyIntel_Surveillance AS
SELECT
    s.Activity_ID,
    s.Activity_Date,
    s.Activity_Type,
    s.Sector,
    s.Approval_Number                        AS AWI,
    s.Organisation_Name,
    s.Location,
    s.Inspector,
    s.Outcome,
    s.Findings_Count
FROM gold.DM_TBL_SRG_SURVEILLANCE_ACTIVITY s;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_Occurrences
-- Grain: one row per safety occurrence (Bird Strike, Runway Incursion, etc.).
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_Occurrences','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_Occurrences;
GO
CREATE VIEW gold.vw_SafetyIntel_Occurrences AS
SELECT
    o.Occurrence_ID,
    o.Occurrence_Date,
    YEAR(o.Occurrence_Date)                  AS Occurrence_Year,
    o.Activity_Code,
    o.Occurrence_Subtype,
    o.Approval_Number                        AS AWI,
    o.Organisation_Name,
    o.Aircraft_Registration,
    o.Location,
    o.CE_Mapping,
    o.Finding_Level,
    o.Lead_Inspector,
    o.Target_Close_Date,
    o.Current_Status,
    o.ESOMS_Reference_No,
    o.Summary
FROM gold.DM_TBL_SRG_OCCURRENCES o;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_OccurrenceOpsOverview
-- Grain: one row per activity code. Aggregated dashboard KPI surface.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_OccurrenceOpsOverview','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_OccurrenceOpsOverview;
GO
CREATE VIEW gold.vw_SafetyIntel_OccurrenceOpsOverview AS
SELECT
    t.Activity_Code,
    COUNT(*)                                  AS Active_Tracks,
    CAST(AVG(t.Jamming_Index_Pct) AS DECIMAL(5,2))    AS Jamming_Index_Pct,
    CAST(AVG(t.Integrity_Index_Pct) AS DECIMAL(5,2))  AS Integrity_Index_Pct,
    SUM(CASE WHEN t.Conflict_Alert IS NOT NULL THEN 1 ELSE 0 END) AS Loss_Alert_Count,
    COUNT(*)                                  AS Records_Analyzed,
    MAX(t.Location)                           AS Primary_Location
FROM gold.DM_TBL_SRG_OCCURRENCE_OPS_TRACK t
GROUP BY t.Activity_Code;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_OccurrenceOps
-- Grain: one row per operational track snapshot.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_OccurrenceOps','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_OccurrenceOps;
GO
CREATE VIEW gold.vw_SafetyIntel_OccurrenceOps AS
SELECT
    t.Track_ID,
    t.Snapshot_Timestamp,
    t.Activity_Code,
    t.Approval_Number                        AS AWI,
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

------------------------------------------------------------------------------
-- vw_SafetyIntel_OccurrenceHotspots
-- Grain: one row per hotspot grid cell.
------------------------------------------------------------------------------
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

------------------------------------------------------------------------------
-- vw_SafetyIntel_TacticalAudit
-- Grain: one row per tactical dashboard note.
------------------------------------------------------------------------------
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

------------------------------------------------------------------------------
-- vw_SafetyIntel_ChangeMgmt
-- Grain: one change-management reference per row.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_ChangeMgmt','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_ChangeMgmt;
GO
CREATE VIEW gold.vw_SafetyIntel_ChangeMgmt AS
SELECT
    c.Reference_ID,
    c.Reference_Date,
    c.Category,
    c.Approval_Number                        AS AWI,
    c.Organisation_Name,
    c.Title,
    c.Summary,
    c.Source_Document_URL
FROM gold.DM_TBL_SRG_CHANGE_MGMT_REF c;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_AOC_Applications
-- Grain: one row per AOC application (drives "AOC Applications by Request Type").
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_AOC_Applications','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_AOC_Applications;
GO
CREATE VIEW gold.vw_SafetyIntel_AOC_Applications AS
SELECT
    a.Application_ID,
    a.AOC_Number,
    a.Operator_Name,
    a.Application_Type, -- New AMO / Renewal / Variation
    a.Application_Date,
    a.Decision_Date,
    a.Decision,
    a.Lead_Inspector,
    YEAR(a.Application_Date)                 AS Application_Year
FROM gold.DM_TBL_SRG_AOC_APPLICATIONS a;
GO

------------------------------------------------------------------------------
-- vw_SafetyIntel_TAM
-- Grain: one row per organisation x bilateral agreement.
------------------------------------------------------------------------------
IF OBJECT_ID('gold.vw_SafetyIntel_TAM','V') IS NOT NULL DROP VIEW gold.vw_SafetyIntel_TAM;
GO
CREATE VIEW gold.vw_SafetyIntel_TAM AS
SELECT
    t.Organisation_Name,
    t.Approval_Number                        AS Foreign_Approval_Number,
    t.Type_of_Agreement,
    t.Country,
    t.[Status]
FROM gold.DM_TBL_SRG_AMO_TAM_LIST t;
GO

/***************************************************************************************
 AGENT-SIDE REQUIREMENTS FOR THE GOLD SCHEMA
 ---------------------------------------------------------------------------
 The views above resolve only if the app also targets "gold". As of the current
 build this is handled by a single env var — no code/prompt edits are needed:

 1. Set the Container App env var  SYNAPSE_SQL_SCHEMA=gold  and redeploy.
    The nl2sql tool rewrites every vw_SafetyIntel_* reference to
    gold.vw_SafetyIntel_* before executing, so the hardcoded dbo. query in
    agent.py and any dbo. hints in the prompts are normalised automatically.

 2. Grant the app managed-identity user read access on the gold schema:
        CREATE USER [srgsib-app] FROM EXTERNAL PROVIDER;   -- if not already created
        GRANT SELECT  ON SCHEMA::gold TO [srgsib-app];
        GRANT EXECUTE ON SCHEMA::gold TO [srgsib-app];      -- only if nl2sql calls procs

 3. (Optional) Set the user's default schema; queries are already schema-qualified
    so this is not required:
        ALTER USER [srgsib-app] WITH DEFAULT_SCHEMA = gold;
****************************************************************************************/
