/***************************************************************************************
 Script  : Trimmed Synapse views for the Safety Intelligence Bot (AMO subset, GOLD schema)
 Target  : Azure Synapse Analytics - Dedicated SQL Pool
 Purpose : Subset of "vw_SafetyIntel_*" views that depend ONLY on the five AMO base
           tables a customer has loaded:
             - DM_TBL_SRG_AMO_MOA
             - DM_TBL_SRG_AMO_TIER
             - DM_TBL_SRG_AMO_ASSIGNED_PMI
             - DM_TBL_SRG_AMO_AUDIT_TRACKING
             - DM_TBL_SRG_AMO_TAM_LIST

 GOLD variant:
   - Identical to vw_SafetyIntel_Views_AMO_Subset.sql but every object (view +
     base table) lives in the "gold" schema instead of "dbo".
   - Use this when only the AMO base tables are available AND they were landed
     in the "gold" schema.
   - The nine occurrence/findings/surveillance/AOC/change-mgmt views are
     intentionally omitted because their base tables are not present.

 Supported views (4):
   - vw_SafetyIntel_AMO         (MOA + TIER + ASSIGNED_PMI)
   - vw_SafetyIntel_Audits      (AUDIT_TRACKING)
   - vw_SafetyIntel_TierTrend   (TIER + MOA)
   - vw_SafetyIntel_TAM         (TAM_LIST)

 Notes:
   - All views are read-only logical views. The agent's SQL allow-list permits
     SELECT only on objects whose name starts with "vw_SafetyIntel_".
   - Synapse does not enforce FKs; joins are by AWI / Approval_Number.
   - See the AGENT-SIDE CHANGES note at the bottom of vw_SafetyIntel_Views_Gold.sql.
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
