-- 00_schemas.sql
-- the five schemas: raw landing tables (stg), cleaned data (core), model-ready tables (mart),
-- and run bookkeeping (etl). safe to run any number of times.

IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');
GO
IF SCHEMA_ID('core') IS NULL EXEC('CREATE SCHEMA core');
GO
IF SCHEMA_ID('mart') IS NULL EXEC('CREATE SCHEMA mart');
GO
IF SCHEMA_ID('etl') IS NULL EXEC('CREATE SCHEMA etl');
GO
