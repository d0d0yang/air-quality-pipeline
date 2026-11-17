-- 30_marts.sql
-- mart.daily_station: one row per site per local day (America/Toronto, so daylight time is handled by
-- AT TIME ZONE). it holds whole-day stats, used as targets and as "yesterday" features, plus "to6" stats
-- over the window that ends at 06:00 local, which is all a forecast issued that morning is allowed to see.
-- incremental runs only rebuild the days this run touched and everything after them.

-- hourly aqhi computed from 3-hour trailing means of the best available no2, o3 and pm2.5 for each site.
-- a mean needs 2 of the 3 hours inside a 3-hour span. returns hour-beginning utc like everything else.
CREATE OR ALTER FUNCTION mart.fn_aqhi_hourly (@from_utc DATETIME2(0), @to_utc DATETIME2(0))
RETURNS TABLE
AS
RETURN
WITH roll AS (
    SELECT site_id, pollutant, hour_utc,
           AVG(CAST(value AS FLOAT)) OVER (PARTITION BY site_id, pollutant ORDER BY hour_utc
                                           ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS mean3,
           COUNT(*) OVER (PARTITION BY site_id, pollutant ORDER BY hour_utc ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS n3,
           MIN(hour_utc) OVER (PARTITION BY site_id, pollutant ORDER BY hour_utc
                               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS first3
    FROM core.v_measurement_best
    WHERE pollutant IN ('no2', 'o3', 'pm25')
      AND hour_utc >= DATEADD(HOUR, -3, @from_utc) AND hour_utc < @to_utc
),
pivoted AS (
    SELECT site_id, hour_utc,
           MAX(IIF(pollutant = 'no2', mean3, NULL)) AS no2,
           MAX(IIF(pollutant = 'o3', mean3, NULL)) AS o3,
           MAX(IIF(pollutant = 'pm25', mean3, NULL)) AS pm25
    FROM roll
    WHERE n3 >= 2 AND DATEDIFF(HOUR, first3, hour_utc) <= 2 AND hour_utc >= @from_utc
    GROUP BY site_id, hour_utc
)
-- stieb et al. 2008 / eccc, same coefficients as pipeline/aqhi.py
SELECT site_id, hour_utc,
       10.0 / 10.4 * 100 * ((EXP(0.000871 * no2) - 1) + (EXP(0.000537 * o3) - 1) + (EXP(0.000487 * pm25) - 1)) AS aqhi
FROM pivoted
WHERE no2 IS NOT NULL AND o3 IS NOT NULL AND pm25 IS NOT NULL;
GO

-- the full history, for the dashboard and the dq check
CREATE OR ALTER VIEW mart.v_aqhi_hourly
AS
SELECT site_id, hour_utc, aqhi FROM mart.fn_aqhi_hourly('2000-01-01', '2100-01-01');
GO

CREATE OR ALTER PROCEDURE mart.usp_refresh
    @run_id INT,
    @rebuild_all BIT = 0   -- maintenance: rebuild every day (e.g. after fixing station matching)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @mode VARCHAR(12) = (SELECT mode FROM etl.pipeline_run WHERE run_id = @run_id);
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();
    DECLARE @from_utc DATETIME2(0);

    IF @rebuild_all = 1
        SET @mode = 'full';
    IF @mode = 'full'
        SET @from_utc = (SELECT MIN(hour_utc) FROM core.measurement);
    ELSE
        -- earliest hour this run changed, but always at least the last 4 days (weather and forecasts move too)
        SET @from_utc = (SELECT MIN(h) FROM (VALUES
                            ((SELECT MIN(hour_utc) FROM core.measurement WHERE last_changed_run = @run_id)),
                            (DATEADD(DAY, -4, @now))) AS v(h));
    SET @from_utc = ISNULL(@from_utc, DATEADD(DAY, -4, @now));

    -- the first local date to rebuild: the day of the earliest changed hour. later days follow because a day's
    -- row also reads the evening before it (the to6 window); #h below starts two days earlier to cover that.
    DECLARE @d0 DATE = CAST(CAST(@from_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS DATE);
    DECLARE @to_utc DATETIME2(0) = DATEADD(DAY, 4, @now);

    -- hourly values with their local day and the forecast "issue day" they feed:
    -- hours in [D-1 06:00, D 06:00) local belong to issue day D (local time + 18 h lands on day D)
    SELECT b.site_id, b.pollutant, b.hour_utc, CAST(b.value AS FLOAT) AS value,
           CAST(t.lt AS DATE) AS local_date,
           CAST(DATEADD(HOUR, 18, t.lt) AS DATE) AS issue_date,
           DATEPART(HOUR, t.lt) AS local_hour
    INTO #h
    FROM core.v_measurement_best AS b
    CROSS APPLY (SELECT CAST(b.hour_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS lt) AS t
    WHERE b.hour_utc >= DATEADD(DAY, -2, @from_utc) AND b.pollutant IN ('pm25', 'o3', 'no2');

    -- o3: 8-hour trailing means (6 of 8 hours inside an 8-hour span), stamped at the last hour
    SELECT site_id, local_date, issue_date, o3_8h
    INTO #o3
    FROM (
        SELECT site_id, local_date, issue_date, hour_utc,
               AVG(value) OVER (PARTITION BY site_id ORDER BY hour_utc ROWS BETWEEN 7 PRECEDING AND CURRENT ROW) AS o3_8h,
               COUNT(*) OVER (PARTITION BY site_id ORDER BY hour_utc ROWS BETWEEN 7 PRECEDING AND CURRENT ROW) AS n8,
               MIN(hour_utc) OVER (PARTITION BY site_id ORDER BY hour_utc ROWS BETWEEN 7 PRECEDING AND CURRENT ROW) AS first8
        FROM #h WHERE pollutant = 'o3'
    ) AS r
    WHERE n8 >= 6 AND DATEDIFF(HOUR, first8, hour_utc) <= 7;

    SELECT site_id, local_date,
           AVG(IIF(pollutant = 'pm25', value, NULL)) AS pm25_mean,
           MAX(IIF(pollutant = 'pm25', value, NULL)) AS pm25_max,
           SUM(IIF(pollutant = 'pm25', 1, 0)) AS pm25_hours,
           AVG(IIF(pollutant = 'no2', value, NULL)) AS no2_mean
    INTO #day
    FROM #h GROUP BY site_id, local_date;

    SELECT site_id, issue_date,
           AVG(IIF(pollutant = 'pm25', value, NULL)) AS pm25_24h_mean,
           MAX(IIF(pollutant = 'pm25', value, NULL)) AS pm25_24h_max,
           SUM(IIF(pollutant = 'pm25', 1, 0)) AS pm25_24h_hours,
           AVG(IIF(pollutant = 'pm25' AND local_date = issue_date AND local_hour < 6, value, NULL)) AS pm25_6h_mean,
           AVG(IIF(pollutant = 'no2', value, NULL)) AS no2_24h_mean
    INTO #to6
    FROM #h GROUP BY site_id, issue_date;

    SELECT a.site_id, CAST(CAST(a.hour_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS DATE) AS local_date,
           MAX(a.aqhi) AS aqhi_max
    INTO #aqhi
    FROM mart.fn_aqhi_hourly(DATEADD(DAY, -1, @from_utc), @to_utc) AS a
    GROUP BY a.site_id, CAST(CAST(a.hour_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS DATE);

    -- observed weather from the matched climate station (only matches within MAX_MATCH_KM)
    SELECT m.station_id AS site_id,
           CAST(CAST(w.hour_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS DATE) AS local_date,
           AVG(CAST(w.temp_c AS FLOAT)) AS temp_mean, MAX(w.temp_c) AS temp_max,
           AVG(CAST(w.wind_kmh AS FLOAT)) AS wind_mean, AVG(CAST(w.rh_pct AS FLOAT)) AS rh_mean,
           SUM(w.precip_mm) AS precip_sum
    INTO #obs
    FROM core.station_weather_match AS m
    JOIN core.weather_hourly AS w ON w.climate_id = m.climate_id
    WHERE m.is_used = 1 AND w.hour_utc >= DATEADD(DAY, -2, @from_utc)
    GROUP BY m.station_id, CAST(CAST(w.hour_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS DATE);

    -- forecast weather at the site's open-meteo grid point (archived for the past, live for tomorrow)
    SELECT m.station_id AS site_id,
           CAST(CAST(f.hour_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS DATE) AS local_date,
           AVG(CAST(f.temp_c AS FLOAT)) AS temp_mean, MAX(f.temp_c) AS temp_max,
           AVG(CAST(f.wind_kmh AS FLOAT)) AS wind_mean,
           AVG(SIN(RADIANS(CAST(f.wind_dir_deg AS FLOAT)))) AS wind_sin,
           AVG(COS(RADIANS(CAST(f.wind_dir_deg AS FLOAT)))) AS wind_cos,
           AVG(CAST(f.rh_pct AS FLOAT)) AS rh_mean, SUM(f.precip_mm) AS precip_sum,
           AVG(CAST(f.cloud_pct AS FLOAT)) AS cloud_mean, MIN(f.blh_m) AS blh_min,
           MAX(f.kind) AS fc_kind  -- 'live' sorts after 'hist', so any live hour marks the day as live
    INTO #fc
    FROM core.station_weather_match AS m
    JOIN core.weather_forecast_hourly AS f ON f.point_id = m.point_id
    WHERE f.hour_utc >= DATEADD(DAY, -2, @from_utc)
    GROUP BY m.station_id, CAST(CAST(f.hour_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0)) AS DATE);

    -- which rows to build: every site-day with measurements, plus the next days for sites active this month
    -- (those rows carry tomorrow's forecast weather for prediction)
    DECLARE @today DATE = CAST(@now AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATE);
    SELECT site_id, local_date INTO #keys FROM #day
    UNION
    SELECT a.site_id, f.local_date
    FROM (SELECT DISTINCT site_id FROM #h WHERE hour_utc >= DATEADD(DAY, -30, @now)) AS a
    JOIN #fc AS f ON f.site_id = a.site_id AND f.local_date BETWEEN DATEADD(DAY, -1, @today) AND DATEADD(DAY, 2, @today);

    BEGIN TRANSACTION;
    IF @mode = 'full'
        TRUNCATE TABLE mart.daily_station;
    ELSE
        DELETE FROM mart.daily_station WHERE local_date >= @d0;

    INSERT INTO mart.daily_station (
        site_id, local_date, pm25_mean, pm25_max, pm25_hours, pm25_completeness, o3_8h_max, no2_mean, aqhi_max,
        pm25_24h_to6_mean, pm25_24h_to6_max, pm25_24h_to6_hours, pm25_6h_to6_mean, o3_8h_to6_max, no2_24h_to6_mean,
        obs_temp_mean, obs_temp_max, obs_wind_mean, obs_rh_mean, obs_precip_sum,
        fc_temp_mean, fc_temp_max, fc_wind_mean, fc_wind_sin, fc_wind_cos, fc_rh_mean, fc_precip_sum, fc_cloud_mean,
        fc_blh_min, fc_kind, updated_run)
    SELECT k.site_id, k.local_date,
           d.pm25_mean, d.pm25_max, NULLIF(d.pm25_hours, 0), 100.0 * NULLIF(d.pm25_hours, 0) / 24, o.o3_8h_max,
           d.no2_mean, q.aqhi_max,
           t.pm25_24h_mean, t.pm25_24h_max, NULLIF(t.pm25_24h_hours, 0), t.pm25_6h_mean, o6.o3_8h_max, t.no2_24h_mean,
           ob.temp_mean, ob.temp_max, ob.wind_mean, ob.rh_mean, ob.precip_sum,
           f.temp_mean, f.temp_max, f.wind_mean, f.wind_sin, f.wind_cos, f.rh_mean, f.precip_sum, f.cloud_mean,
           f.blh_min, f.fc_kind, @run_id
    FROM #keys AS k
    LEFT JOIN #day AS d ON d.site_id = k.site_id AND d.local_date = k.local_date
    LEFT JOIN #to6 AS t ON t.site_id = k.site_id AND t.issue_date = k.local_date
    LEFT JOIN (SELECT site_id, local_date, MAX(o3_8h) AS o3_8h_max FROM #o3 GROUP BY site_id, local_date) AS o
        ON o.site_id = k.site_id AND o.local_date = k.local_date
    LEFT JOIN (SELECT site_id, issue_date, MAX(o3_8h) AS o3_8h_max FROM #o3 GROUP BY site_id, issue_date) AS o6
        ON o6.site_id = k.site_id AND o6.issue_date = k.local_date
    LEFT JOIN #aqhi AS q ON q.site_id = k.site_id AND q.local_date = k.local_date
    LEFT JOIN #obs AS ob ON ob.site_id = k.site_id AND ob.local_date = k.local_date
    LEFT JOIN #fc AS f ON f.site_id = k.site_id AND f.local_date = k.local_date
    WHERE k.local_date >= @d0;
    DECLARE @written BIGINT = @@ROWCOUNT;
    COMMIT;

    INSERT INTO etl.stage_run (run_id, stage, status, started_at, ended_at, seconds, rows_written, message)
    VALUES (@run_id, 'mart', 'succeeded', @started, SYSUTCDATETIME(),
            DATEDIFF_BIG(MILLISECOND, @started, SYSUTCDATETIME()) / 1000.0, @written,
            CONCAT('rebuilt local dates from ', @d0));
END;
GO
