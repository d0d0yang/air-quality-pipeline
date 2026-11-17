-- 90_users.sql
-- the least-privilege user every runtime connects as (functions, adf, local runs). it can read and write
-- the data schemas and run the procs, but can't change the schema. $(ETL_PASSWORD) is filled in by
-- `python -m pipeline.db apply` from an env var and never stored in this file.
-- on azure sql this is a contained user; locally the container enables contained auth first (see db.py).

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'etl_app')
    CREATE USER etl_app WITH PASSWORD = '$(ETL_PASSWORD)';
ELSE
    ALTER USER etl_app WITH PASSWORD = '$(ETL_PASSWORD)';
GO

GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::stg TO etl_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::core TO etl_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::mart TO etl_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::etl TO etl_app;
GRANT EXECUTE ON SCHEMA::stg TO etl_app;
GRANT EXECUTE ON SCHEMA::core TO etl_app;
GRANT EXECUTE ON SCHEMA::mart TO etl_app;
GRANT EXECUTE ON SCHEMA::etl TO etl_app;
-- mart.usp_refresh truncates mart.daily_station on full runs
GRANT ALTER ON OBJECT::mart.daily_station TO etl_app;
GO
