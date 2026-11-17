-- 40_run_procs.sql
-- run bookkeeping: start and end a run, log a stage, apply the watermarks an extract returned, and rewind
-- watermarks by a day for the refresh benchmark. the same procs are called by run.py and by adf.

-- starts a run and returns its run_id. full mode empties the fact tables and watermarks first.
-- stations are kept (merged, not truncated) so site ids stay stable for the forecast history,
-- and the official aqhi archive is kept because eccc only serves ~3 days of it.
CREATE OR ALTER PROCEDURE etl.usp_start_run
    @mode VARCHAR(12), @target VARCHAR(10) = 'local', @trigger_name NVARCHAR(100) = NULL
WITH EXECUTE AS OWNER  -- truncate needs alter permission, which etl_app doesn't have
AS
BEGIN
    SET NOCOUNT ON;
    IF @mode NOT IN ('full', 'incremental')
        THROW 50010, 'mode must be full or incremental', 1;

    -- runs left 'running' for over 6 hours died without reaching usp_end_run
    UPDATE etl.pipeline_run
    SET status = 'failed', ended_at = SYSUTCDATETIME(), error_message = N'abandoned: no end_run within 6 hours'
    WHERE status = 'running' AND started_at < DATEADD(HOUR, -6, SYSUTCDATETIME());

    IF EXISTS (SELECT 1 FROM etl.pipeline_run WHERE status = 'running')
        THROW 50011, 'another pipeline run is still running', 1;

    IF @mode = 'full'
    BEGIN
        TRUNCATE TABLE core.measurement;
        TRUNCATE TABLE core.weather_hourly;
        TRUNCATE TABLE core.weather_forecast_hourly;
        DELETE FROM etl.sensor_state;
        DELETE FROM etl.source_state WHERE source_id NOT IN ('aqhi_obs', 'aqhi_forecast');
        TRUNCATE TABLE stg.measurement;
        TRUNCATE TABLE stg.climate_hourly;
        TRUNCATE TABLE stg.weather_forecast;
    END;

    INSERT INTO etl.pipeline_run (mode, target, trigger_name, status, started_at)
    VALUES (@mode, @target, @trigger_name, 'running', SYSUTCDATETIME());
    SELECT CAST(SCOPE_IDENTITY() AS INT) AS run_id;
END;
GO

CREATE OR ALTER PROCEDURE etl.usp_log_stage
    @run_id INT, @stage VARCHAR(20), @source_id VARCHAR(40) = NULL, @item_id NVARCHAR(200) = NULL,
    @status VARCHAR(12) = 'succeeded', @seconds DECIMAL(12, 3) = NULL, @rows_read BIGINT = NULL,
    @rows_written BIGINT = NULL, @message NVARCHAR(4000) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO etl.stage_run (run_id, stage, source_id, item_id, status, started_at, ended_at, seconds,
                               rows_read, rows_written, message)
    VALUES (@run_id, @stage, @source_id, @item_id, @status,
            DATEADD(MILLISECOND, -CAST(ISNULL(@seconds, 0) * 1000 AS INT), SYSUTCDATETIME()), SYSUTCDATETIME(),
            @seconds, @rows_read, @rows_written, @message);
END;
GO

-- closes a run. called on success and on every failure path (adf wires each activity's failure output here).
CREATE OR ALTER PROCEDURE etl.usp_end_run
    @run_id INT, @status VARCHAR(12), @error_message NVARCHAR(4000) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    WITH totals AS (
        SELECT SUM(IIF(stage = 'extract', rows_read, 0)) AS rows_read,
               SUM(IIF(stage = 'extract', rows_written, 0)) AS rows_written,
               SUM(IIF(stage = 'merge', rows_inserted, 0)) AS rows_inserted,
               SUM(IIF(stage = 'merge', rows_updated, 0)) AS rows_updated,
               SUM(IIF(stage = 'extract', 1, 0)) AS items
        FROM etl.stage_run WHERE run_id = @run_id
    )
    UPDATE r
    SET status = @status, ended_at = SYSUTCDATETIME(), error_message = LEFT(@error_message, 4000),
        rows_read = t.rows_read, rows_written = t.rows_written, rows_inserted = t.rows_inserted,
        rows_updated = t.rows_updated, items_planned = ISNULL(r.items_planned, t.items),
        measurement_rows = (SELECT COUNT_BIG(*) FROM core.measurement)
    FROM etl.pipeline_run AS r CROSS JOIN totals AS t
    WHERE r.run_id = @run_id AND r.status = 'running';
END;
GO

CREATE OR ALTER PROCEDURE etl.usp_set_items_planned
    @run_id INT, @items INT
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE etl.pipeline_run SET items_planned = @items WHERE run_id = @run_id;
END;
GO

-- applies the new_watermark json an extract returned, only after that item's merge succeeded:
-- {"source_state": [{"source_id", "state_key", "value_text", "value_utc"}],
--  "sensor_state": [{"sensor_id", "from", "to", "finished", "first_segment", "last_hour_utc"}]}
-- a sensor watermark only moves over contiguous data, so a failed item is retried next run instead of skipped.
CREATE OR ALTER PROCEDURE etl.usp_apply_watermarks
    @run_id INT, @json NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    IF @json IS NULL OR ISJSON(@json) = 0
        RETURN;

    MERGE etl.source_state AS t
    USING (
        SELECT source_id, state_key, value_text, TRY_CONVERT(DATETIME2(0), LEFT(value_utc, 19), 126) AS value_utc
        FROM OPENJSON(@json, '$.source_state')
             WITH (source_id VARCHAR(40), state_key NVARCHAR(400), value_text NVARCHAR(400), value_utc NVARCHAR(40))
    ) AS s ON t.source_id = s.source_id AND t.state_key = s.state_key
    WHEN MATCHED THEN
        -- datetime watermarks never move backwards (a rewind is done on purpose by the benchmark proc only)
        UPDATE SET value_text = s.value_text,
                   value_utc = IIF(t.value_utc IS NOT NULL AND s.value_utc < t.value_utc, t.value_utc, s.value_utc),
                   updated_run = @run_id, updated_at = SYSUTCDATETIME()
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (source_id, state_key, value_text, value_utc, updated_run, updated_at)
        VALUES (s.source_id, s.state_key, s.value_text, s.value_utc, @run_id, SYSUTCDATETIME());

    -- sensors: a finished segment means "fetched through to", even if the sensor had no data in it
    DECLARE @seg TABLE (sensor_id INT, seg_from DATETIME2(0), progress DATETIME2(0), first_segment BIT);
    INSERT INTO @seg
    SELECT sensor_id, TRY_CONVERT(DATETIME2(0), LEFT(seg_from, 19), 126),
           CASE WHEN finished = 1 THEN DATEADD(HOUR, -1, TRY_CONVERT(DATETIME2(0), LEFT(seg_to, 19), 126))
                ELSE TRY_CONVERT(DATETIME2(0), LEFT(last_hour_utc, 19), 126) END,
           ISNULL(first_segment, 0)
    FROM OPENJSON(@json, '$.sensor_state')
         WITH (sensor_id INT, seg_from NVARCHAR(40) '$.from', seg_to NVARCHAR(40) '$.to', finished BIT,
               first_segment BIT, last_hour_utc NVARCHAR(40));

    MERGE etl.sensor_state AS t
    USING (SELECT sensor_id, MIN(seg_from) AS seg_from, MAX(progress) AS progress, MAX(CAST(first_segment AS INT)) AS first_segment
           FROM @seg WHERE progress IS NOT NULL GROUP BY sensor_id) AS s
        ON t.sensor_id = s.sensor_id
    WHEN MATCHED AND s.seg_from <= DATEADD(HOUR, 1, t.last_hour_utc) AND s.progress > t.last_hour_utc THEN
        UPDATE SET last_hour_utc = s.progress, updated_run = @run_id, updated_at = SYSUTCDATETIME()
    WHEN NOT MATCHED BY TARGET AND s.first_segment = 1 THEN
        INSERT (sensor_id, last_hour_utc, updated_run, updated_at)
        VALUES (s.sensor_id, s.progress, @run_id, SYSUTCDATETIME());
END;
GO

-- benchmark helper: rewinds every time-based watermark by 24 hours, so the next incremental run re-pulls
-- and re-merges a real day of data (the "incremental_daily" scenario in benchmarks/refresh_benchmark.py)
CREATE OR ALTER PROCEDURE etl.usp_prepare_benchmark_day
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE etl.sensor_state SET last_hour_utc = DATEADD(HOUR, -24, last_hour_utc), updated_at = SYSUTCDATETIME();
    UPDATE etl.source_state SET value_utc = DATEADD(HOUR, -24, value_utc), updated_at = SYSUTCDATETIME()
    WHERE value_utc IS NOT NULL;
    -- open-meteo stores the last date it loaded per point as text
    UPDATE etl.source_state
    SET value_text = CONVERT(CHAR(10), DATEADD(DAY, -1, TRY_CONVERT(DATE, value_text)), 23), updated_at = SYSUTCDATETIME()
    WHERE source_id = 'openmeteo_hist' AND TRY_CONVERT(DATE, value_text) IS NOT NULL;
    SELECT (SELECT COUNT(*) FROM etl.sensor_state) AS sensors_rewound,
           (SELECT COUNT(*) FROM etl.source_state WHERE value_utc IS NOT NULL OR source_id = 'openmeteo_hist') AS sources_rewound;
END;
GO
