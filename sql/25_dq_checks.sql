-- 25_dq_checks.sql
-- the analysis view with source precedence, and the named data-quality checks that run at the end of every
-- pipeline run. each check writes one row to etl.dq_result; any failed 'error' check fails the run.

-- one value per site, pollutant and hour. precedence: naps (validated) > openaq government monitor > low-cost sensor
CREATE OR ALTER VIEW core.v_measurement_best
AS
WITH ranked AS (
    SELECT x.site_id, m.pollutant, m.hour_utc, m.value, m.source, s.is_low_cost_sensor, m.station_id,
           ROW_NUMBER() OVER (
               PARTITION BY x.site_id, m.pollutant, m.hour_utc
               ORDER BY CASE WHEN m.source = 'naps' THEN 1 WHEN s.is_low_cost_sensor = 0 THEN 2 ELSE 3 END,
                        m.station_id) AS rn
    FROM core.measurement AS m
    JOIN core.station AS s ON s.station_id = m.station_id
    JOIN core.station_xref AS x ON x.station_id = m.station_id
    WHERE m.value IS NOT NULL
)
SELECT site_id, pollutant, hour_utc, value, source, is_low_cost_sensor, station_id
FROM ranked
WHERE rn = 1;
GO

CREATE OR ALTER PROCEDURE etl.usp_add_dq
    @run_id INT, @check_name VARCHAR(60), @severity VARCHAR(10), @failed BIT, @observed DECIMAL(18, 4),
    @threshold DECIMAL(18, 4), @detail NVARCHAR(1000)
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO etl.dq_result (run_id, check_name, severity, status, observed, threshold, detail, checked_at)
    VALUES (@run_id, @check_name, @severity,
            CASE WHEN @failed = 0 THEN 'pass' WHEN @severity = 'error' THEN 'fail' ELSE 'warn' END,
            @observed, @threshold, @detail, SYSUTCDATETIME());
END;
GO

CREATE OR ALTER PROCEDURE etl.usp_run_dq_checks
    @run_id INT, @max_match_km FLOAT = 25, @max_aqhi_mae FLOAT = 0.5
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @n DECIMAL(18, 4), @f BIT, @t DECIMAL(18, 4);
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();
    DECLARE @mode VARCHAR(12) = (SELECT mode FROM etl.pipeline_run WHERE run_id = @run_id);

    -- 1. nothing in the future (error)
    SET @n = (SELECT COUNT(*) FROM core.measurement WHERE hour_utc > DATEADD(HOUR, 1, @now));
    SET @f = IIF(@n > 0, 1, 0);
    EXEC etl.usp_add_dq @run_id, 'no_future_measurements', 'error', @f, @n, 0, N'measurement hours later than now + 1 h';

    -- 2. every site that reported in the last 30 days also reported in the last 72 h (warn)
    WITH recent AS (
        SELECT x.site_id, MAX(m.hour_utc) AS last_hour
        FROM core.measurement AS m JOIN core.station_xref AS x ON x.station_id = m.station_id
        WHERE m.hour_utc >= DATEADD(DAY, -30, @now)
        GROUP BY x.site_id
    )
    SELECT @n = COUNT(*) FROM recent WHERE last_hour < DATEADD(HOUR, -72, @now);
    SET @f = IIF(@n > 0, 1, 0);
    EXEC etl.usp_add_dq @run_id, 'active_sites_have_recent_data', 'warn', @f, @n, 0,
         N'sites active in the last 30 days with no data in the last 72 h';

    -- 3. pm2.5 daily completeness: days under 75% are kept but excluded from training (info, logged)
    SET @n = (SELECT COUNT(*) FROM mart.daily_station WHERE pm25_hours IS NOT NULL AND pm25_completeness < 75);
    SET @t = (SELECT COUNT(*) FROM mart.daily_station WHERE pm25_hours IS NOT NULL);
    EXEC etl.usp_add_dq @run_id, 'pm25_days_under_75pct', 'info', 0, @n, @t,
         N'site-days below 75% hourly completeness (filtered out of training); threshold column = all site-days';

    -- 4. a full load didn't lose more than 20% of rows compared with the previous full load (error)
    IF @mode = 'full'
    BEGIN
        DECLARE @prev BIGINT = (SELECT TOP (1) measurement_rows FROM etl.pipeline_run
                                WHERE mode = 'full' AND status = 'succeeded' AND run_id < @run_id AND measurement_rows IS NOT NULL
                                ORDER BY run_id DESC);
        SET @n = (SELECT COUNT_BIG(*) FROM core.measurement);
        SET @f = IIF(@prev IS NOT NULL AND @n < 0.8 * @prev, 1, 0);
        SET @t = ISNULL(0.8 * @prev, 0);
        EXEC etl.usp_add_dq @run_id, 'full_load_row_drop', 'error', @f, @n, @t,
             N'measurement rows vs 80% of the previous successful full load';
    END;

    -- 5. no site uses a weather station further than MAX_MATCH_KM (error)
    SET @n = (SELECT COUNT(*) FROM core.station_weather_match WHERE is_used = 1 AND climate_distance_km > @max_match_km);
    SET @f = IIF(@n > 0, 1, 0);
    SET @t = @max_match_km;
    EXEC etl.usp_add_dq @run_id, 'weather_match_within_max_km', 'error', @f, @n, @t,
         N'sites using a climate station further than MAX_MATCH_KM';

    -- 6. our computed aqhi vs the official one over the last 72 h, for linked sites (warn)
    WITH official AS (
        SELECT x.site_id, o.observation_utc, o.aqhi
        FROM core.aqhi_obs AS o
        JOIN core.station AS a ON a.station_key = CONCAT('aqhi:', o.location_id)
        JOIN core.station_xref AS x ON x.station_id = a.station_id AND x.link_method = 'aqhi_name'
        WHERE o.observation_utc >= DATEADD(HOUR, -72, @now) AND o.aqhi IS NOT NULL
    )
    SELECT @n = AVG(ABS(c.aqhi - o.aqhi)), @t = COUNT(*)
    FROM official AS o
    -- eccc stamps the end of the last averaged hour; our hours are hour-beginning
    JOIN mart.v_aqhi_hourly AS c ON c.site_id = o.site_id AND c.hour_utc = DATEADD(HOUR, -1, o.observation_utc);
    SET @f = IIF(@n IS NOT NULL AND @n > @max_aqhi_mae, 1, 0);
    EXEC etl.usp_add_dq @run_id, 'computed_vs_official_aqhi_mae', 'warn', @f, @n, @max_aqhi_mae,
         N'mean abs difference over the last 72 h at linked sites';

    -- 7. merges that rejected rows during this run (warn, details already in dq_result rows named type_conversion)
    SET @n = (SELECT ISNULL(SUM(rows_rejected), 0) FROM etl.stage_run WHERE run_id = @run_id AND stage = 'merge');
    SET @f = IIF(@n > 0, 1, 0);
    EXEC etl.usp_add_dq @run_id, 'rows_rejected_in_merges', 'warn', @f, @n, 0, N'rows that failed typing or had no station';

    INSERT INTO etl.stage_run (run_id, stage, status, started_at, ended_at, seconds, message)
    SELECT @run_id, 'dq', IIF(SUM(IIF(status = 'fail', 1, 0)) > 0, 'failed', 'succeeded'), @started, SYSUTCDATETIME(),
           DATEDIFF_BIG(MILLISECOND, @started, SYSUTCDATETIME()) / 1000.0,
           CONCAT(SUM(IIF(status = 'pass', 1, 0)), ' pass, ', SUM(IIF(status = 'warn', 1, 0)), ' warn, ',
                  SUM(IIF(status = 'fail', 1, 0)), ' fail')
    FROM etl.dq_result WHERE run_id = @run_id;

    IF EXISTS (SELECT 1 FROM etl.dq_result WHERE run_id = @run_id AND status = 'fail')
    BEGIN
        DECLARE @msg NVARCHAR(2000) = (SELECT STRING_AGG(check_name, ', ') FROM etl.dq_result
                                       WHERE run_id = @run_id AND status = 'fail');
        SET @msg = CONCAT(N'data quality checks failed: ', @msg);
        THROW 50001, @msg, 1;
    END;
END;
GO
