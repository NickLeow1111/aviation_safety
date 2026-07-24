/***************************************************************************************
 Script  : Migrate SafetyIntel objects from the dbo schema to the gold schema
 Target  : Azure Synapse Analytics - Dedicated SQL Pool
 Purpose : Create the gold schema, drop the old dbo vw_SafetyIntel_* views, and
           MOVE every DM_TBL_SRG_* base table from dbo into gold using
           ALTER SCHEMA ... TRANSFER (metadata-only move; no data copy).

 Run order:
   1. This script (creates gold + moves the base tables).
   2. SQL/vw_SafetyIntel_Views_Gold.sql (recreates the views in gold).
   3. Grant + set the app user's schema (see doc/Deploy_ContainerApp_Guide.md §9).
   4. Set the Container App env var SYNAPSE_SQL_SCHEMA=gold and redeploy.

 Safety:
   - ALTER SCHEMA TRANSFER moves the object definition + its data into gold; it
     does NOT duplicate storage.
   - The dbo vw_SafetyIntel_* views are dropped first because they reference the
     dbo tables and would break once the tables move.
   - Idempotent: re-running is a no-op once the tables already live in gold.
   - Review the printed plan before committing in production.
****************************************************************************************/

------------------------------------------------------------------------------
-- 1. Ensure the gold schema exists (CREATE SCHEMA must be its own batch)
------------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'gold')
    EXEC('CREATE SCHEMA gold');
GO

------------------------------------------------------------------------------
-- 2. Drop the dbo vw_SafetyIntel_* views (they depend on the dbo tables)
------------------------------------------------------------------------------
DECLARE @drop_sql NVARCHAR(MAX);

-- Dedicated SQL pool does NOT support "SELECT @v += ... FROM <table>";
-- aggregate the statements with STRING_AGG instead.
SELECT @drop_sql = STRING_AGG(
    CAST('DROP VIEW dbo.' + QUOTENAME(v.name) + ';' AS NVARCHAR(MAX)), CHAR(13))
FROM sys.views v
WHERE v.schema_id = SCHEMA_ID('dbo')
  AND v.name LIKE 'vw_SafetyIntel[_]%';

IF (@drop_sql IS NOT NULL)
BEGIN
    PRINT '--- Dropping dbo views ---';
    PRINT @drop_sql;
    EXEC sp_executesql @drop_sql;
END
ELSE
    PRINT 'No dbo.vw_SafetyIntel_* views to drop.';
GO

------------------------------------------------------------------------------
-- 3. Transfer every DM_TBL_SRG_* base table from dbo -> gold
------------------------------------------------------------------------------
DECLARE @move_sql NVARCHAR(MAX);

SELECT @move_sql = STRING_AGG(
    CAST('ALTER SCHEMA gold TRANSFER dbo.' + QUOTENAME(t.name) + ';' AS NVARCHAR(MAX)), CHAR(13))
FROM sys.tables t
WHERE t.schema_id = SCHEMA_ID('dbo')
  AND t.name LIKE 'DM_TBL_SRG[_]%';

IF (@move_sql IS NOT NULL)
BEGIN
    PRINT '--- Transferring base tables dbo -> gold ---';
    PRINT @move_sql;
    EXEC sp_executesql @move_sql;
END
ELSE
    PRINT 'No dbo.DM_TBL_SRG_* tables left to transfer (already in gold?).';
GO

------------------------------------------------------------------------------
-- 4. Verify: base tables should now report schema_name = 'gold'
------------------------------------------------------------------------------
SELECT s.name AS schema_name, t.name AS table_name
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.name LIKE 'DM_TBL_SRG[_]%'
ORDER BY s.name, t.name;
GO

/***************************************************************************************
 NEXT: run SQL/vw_SafetyIntel_Views_Gold.sql to recreate the views in gold.

 ---------------------------------------------------------------------------
 ROLLBACK (move everything back to dbo), if needed:

   -- Drop gold views first
   DECLARE @sql NVARCHAR(MAX);
   SELECT @sql = STRING_AGG(CAST('DROP VIEW gold.' + QUOTENAME(v.name) + ';' AS NVARCHAR(MAX)), CHAR(13))
   FROM sys.views v WHERE v.schema_id = SCHEMA_ID('gold') AND v.name LIKE 'vw_SafetyIntel[_]%';
   IF (@sql IS NOT NULL) EXEC sp_executesql @sql;

   -- Move tables back
   SELECT @sql = STRING_AGG(CAST('ALTER SCHEMA dbo TRANSFER gold.' + QUOTENAME(t.name) + ';' AS NVARCHAR(MAX)), CHAR(13))
   FROM sys.tables t WHERE t.schema_id = SCHEMA_ID('gold') AND t.name LIKE 'DM_TBL_SRG[_]%';
   IF (@sql IS NOT NULL) EXEC sp_executesql @sql;

   -- Then re-run SQL/vw_SafetyIntel_Views.sql (dbo variant).
****************************************************************************************/
