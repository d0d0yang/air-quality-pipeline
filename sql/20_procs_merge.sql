-- 20_procs_merge.sql
-- stg -> core, one proc per landing shape. each one types the text columns with TRY_CONVERT (rows that
-- don't convert are counted as rejected), dedupes with ROW_NUMBER(), MERGEs on the natural key and only
-- updates when row_hash changed, so a re-run is a no-op and a late correction is one update, never a duplicate.
-- full mode doesn't need anything special here: etl.usp_start_run empties the fact tables first.
-- every proc logs inserted/updated/rejected to etl.stage_run and clears its item's stg rows when done.

CREATE OR ALTER PROCEDURE etl.usp_log_merge
    @run_id INT, @source_id VARCHAR(40), @item_id NVARCHAR(200), @started DATETIME2(3),
    @rows_read BIGINT, @inserted BIGINT, @updated BIGINT, @rejected BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO etl.stage_run (run_id, stage, source_id, item_id, status, started_at, ended_at, seconds,
                               rows_read, rows_inserted, rows_updated, rows_rejected)
    VALUES (@run_id, 'merge', @source_id, @item_id, 'succeeded', @started, SYSUTCDATETIME(),
            DATEDIFF_BIG(MILLISECOND, @started, SYSUTCDATETIME()) / 1000.0,
            @rows_read, @inserted, @updated, @rejected);

    IF @rejected > 0
        INSERT INTO etl.dq_result (run_id, check_name, severity, status, observed, threshold, detail, checked_at)
        VALUES (@run_id, 'type_conversion', 'warn', 'warn', @rejected, 0,
                CONCAT(@source_id, ' ', @item_id, ': rows that failed typing or had no known station'),
                SYSUTCDATETIME());

    SELECT @inserted AS rows_inserted, @updated AS rows_updated, @rejected AS rows_rejected;
END;
GO

-------------------------------------------------------------------------------------------------------------
-- measurements (naps_hourly and openaq_hours)
-------------------------------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE core.usp_merge_measurement
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.measurement WHERE item_id = @item_id);
    DECLARE @source_id VARCHAR(40) = (SELECT TOP (1) CONCAT(source, '_', IIF(source = 'naps', 'hourly', 'hours'))
                                      FROM stg.measurement WHERE item_id = @item_id);

    WITH typed AS (
        SELECT st.station_id,
               CAST(s.pollutant AS VARCHAR(10)) AS pollutant,
               TRY_CONVERT(DATETIME2(0), LEFT(s.hour_utc, 19), 126) AS hour_utc,
               CAST(s.source AS VARCHAR(10)) AS source,
               TRY_CONVERT(DECIMAL(10, 3), TRY_CONVERT(FLOAT, NULLIF(s.value, ''))) AS value,
               TRY_CONVERT(DECIMAL(6, 2), TRY_CONVERT(FLOAT, NULLIF(s.coverage_pct, ''))) AS coverage_pct,
               CAST(NULLIF(s.method_code, '') AS VARCHAR(20)) AS method_code,
               TRY_CONVERT(INT, NULLIF(s.sensor_id, '')) AS sensor_id,
               CAST(NULLIF(s.qc_flag, '') AS VARCHAR(50)) AS qc_flag
        FROM stg.measurement AS s
        JOIN core.station AS st ON st.station_key = CONCAT(s.source, ':', s.station_ref)
        WHERE s.item_id = @item_id
    ),
    ranked AS (
        -- one row per key: prefer a real value, then better coverage, then a stable tie-break
        -- (naps sometimes reports one station-hour under two method codes)
        SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY station_id, pollutant, hour_utc, source
                      ORDER BY IIF(value IS NULL, 1, 0), coverage_pct DESC, method_code, sensor_id) AS rn
        FROM typed
        WHERE hour_utc IS NOT NULL
    ),
    src AS (
        SELECT station_id, pollutant, hour_utc, source, value, coverage_pct, method_code, sensor_id, qc_flag,
               HASHBYTES('SHA2_256', CONCAT(value, '|', coverage_pct, '|', method_code, '|', sensor_id, '|', qc_flag)) AS row_hash
        FROM ranked
        WHERE rn = 1
    )
    MERGE core.measurement WITH (HOLDLOCK) AS t
    USING src AS s
        ON t.station_id = s.station_id AND t.pollutant = s.pollutant AND t.hour_utc = s.hour_utc AND t.source = s.source
    WHEN MATCHED AND t.row_hash <> s.row_hash THEN
        UPDATE SET value = s.value, coverage_pct = s.coverage_pct, method_code = s.method_code,
                   sensor_id = s.sensor_id, qc_flag = s.qc_flag, row_hash = s.row_hash, last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (station_id, pollutant, hour_utc, source, value, coverage_pct, method_code, sensor_id, qc_flag,
                row_hash, first_seen_run, last_changed_run)
        VALUES (s.station_id, s.pollutant, s.hour_utc, s.source, s.value, s.coverage_pct, s.method_code,
                s.sensor_id, s.qc_flag, s.row_hash, @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    -- rejected = rows with no known station or an unreadable hour (duplicates aren't rejections)
    DECLARE @valid BIGINT = (
        SELECT COUNT(*) FROM stg.measurement AS s
        JOIN core.station AS st ON st.station_key = CONCAT(s.source, ':', s.station_ref)
        WHERE s.item_id = @item_id AND TRY_CONVERT(DATETIME2(0), LEFT(s.hour_utc, 19), 126) IS NOT NULL);

    DELETE FROM stg.measurement WHERE item_id = @item_id;
    DECLARE @rejected BIGINT = @read - @valid;
    EXEC etl.usp_log_merge @run_id, @source_id, @item_id, @started, @read, @inserted, @updated, @rejected;
END;
GO

-------------------------------------------------------------------------------------------------------------
-- stations: naps, openaq locations (+ sensors), aqhi locations
-------------------------------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE core.usp_merge_naps_stations
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.naps_stations WHERE item_id = @item_id);

    WITH src AS (
        SELECT CONCAT('naps:', s.naps_id) AS station_key,
               CAST(s.naps_id AS VARCHAR(40)) AS source_ref,
               s.station_name AS name,
               s.city,
               TRY_CONVERT(DECIMAL(9, 6), s.lat) AS lat,
               TRY_CONVERT(DECIMAL(9, 6), s.lon) AS lon,
               TRY_CONVERT(DECIMAL(8, 1), NULLIF(s.elevation_m, '')) AS elevation_m,
               CAST(NULLIF(s.site_type, '') AS VARCHAR(20)) AS site_type,
               CAST(NULLIF(s.urbanization, '') AS VARCHAR(20)) AS urbanization,
               TRY_CONVERT(DECIMAL(4, 1), s.utc_offset_h) AS utc_offset_h,
               TRY_CONVERT(DATETIME2(0), NULLIF(s.start_date, '')) AS first_utc,
               TRY_CONVERT(DATETIME2(0), NULLIF(s.end_date, '')) AS last_utc
        FROM stg.naps_stations AS s
        WHERE s.item_id = @item_id AND TRY_CONVERT(DECIMAL(9, 6), s.lat) IS NOT NULL
    ),
    hashed AS (
        SELECT *, HASHBYTES('SHA2_256', CONCAT(name, '|', city, '|', lat, '|', lon, '|', elevation_m, '|', site_type,
                                               '|', urbanization, '|', utc_offset_h, '|', first_utc, '|', last_utc)) AS row_hash
        FROM src
    )
    MERGE core.station AS t
    USING hashed AS s ON t.station_key = s.station_key
    WHEN MATCHED AND ISNULL(t.row_hash, 0x) <> s.row_hash THEN
        UPDATE SET name = s.name, city = s.city, lat = s.lat, lon = s.lon, elevation_m = s.elevation_m,
                   site_type = s.site_type, urbanization = s.urbanization, utc_offset_h = s.utc_offset_h,
                   first_utc = s.first_utc, last_utc = s.last_utc, row_hash = s.row_hash, last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (station_key, source, source_ref, name, city, lat, lon, elevation_m, site_type, urbanization,
                utc_offset_h, provider, licence, attribution, is_monitor, is_low_cost_sensor, first_utc, last_utc,
                row_hash, first_seen_run, last_changed_run)
        VALUES (s.station_key, 'naps', s.source_ref, s.name, s.city, s.lat, s.lon, s.elevation_m, s.site_type,
                s.urbanization, s.utc_offset_h, N'Environment and Climate Change Canada (NAPS)',
                N'Open Government Licence - Canada', N'Environment and Climate Change Canada', 1, 0,
                s.first_utc, s.last_utc, s.row_hash, @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DECLARE @valid BIGINT = (SELECT COUNT(*) FROM stg.naps_stations
                             WHERE item_id = @item_id AND TRY_CONVERT(DECIMAL(9, 6), lat) IS NOT NULL);
    DELETE FROM stg.naps_stations WHERE item_id = @item_id;
    DECLARE @rejected BIGINT = @read - @valid;
    EXEC etl.usp_log_merge @run_id, 'naps_stations', @item_id, @started, @read, @inserted, @updated, @rejected;
END;
GO

CREATE OR ALTER PROCEDURE core.usp_merge_openaq_locations
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.openaq_locations WHERE item_id = @item_id);

    -- one stg row per sensor; a station is the location
    WITH loc AS (
        SELECT location_id, MAX(location_name) AS name, MAX(locality) AS city,
               MAX(TRY_CONVERT(DECIMAL(9, 6), lat)) AS lat, MAX(TRY_CONVERT(DECIMAL(9, 6), lon)) AS lon,
               MAX(provider) AS provider, MAX(licences) AS licence, MAX(attribution) AS attribution,
               MAX(TRY_CONVERT(TINYINT, is_monitor)) AS is_monitor,
               MAX(TRY_CONVERT(DATETIME2(0), LEFT(first_utc, 19), 126)) AS first_utc,
               MAX(TRY_CONVERT(DATETIME2(0), LEFT(last_utc, 19), 126)) AS last_utc
        FROM stg.openaq_locations
        WHERE item_id = @item_id
        GROUP BY location_id
    ),
    hashed AS (
        SELECT *, HASHBYTES('SHA2_256', CONCAT(name, '|', city, '|', lat, '|', lon, '|', provider, '|', licence, '|',
                                               attribution, '|', is_monitor, '|', first_utc, '|', last_utc)) AS row_hash
        FROM loc
    )
    MERGE core.station AS t
    USING hashed AS s ON t.station_key = CONCAT('openaq:', s.location_id)
    WHEN MATCHED AND ISNULL(t.row_hash, 0x) <> s.row_hash THEN
        UPDATE SET name = s.name, city = s.city, lat = s.lat, lon = s.lon, provider = s.provider,
                   licence = s.licence, attribution = s.attribution, is_monitor = s.is_monitor,
                   is_low_cost_sensor = 1 - s.is_monitor, first_utc = s.first_utc, last_utc = s.last_utc,
                   row_hash = s.row_hash, last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (station_key, source, source_ref, name, city, lat, lon, provider, licence, attribution, is_monitor,
                is_low_cost_sensor, first_utc, last_utc, row_hash, first_seen_run, last_changed_run)
        VALUES (CONCAT('openaq:', s.location_id), 'openaq', s.location_id, s.name, s.city, s.lat, s.lon, s.provider,
                s.licence, s.attribution, s.is_monitor, 1 - s.is_monitor, s.first_utc, s.last_utc, s.row_hash,
                @run_id, @run_id)
    OUTPUT $action INTO @actions;

    MERGE core.openaq_sensor AS t
    USING (
        SELECT DISTINCT TRY_CONVERT(INT, s.sensor_id) AS sensor_id, st.station_id,
               CAST(s.pollutant AS VARCHAR(10)) AS pollutant, CAST(s.units AS VARCHAR(20)) AS units
        FROM stg.openaq_locations AS s
        JOIN core.station AS st ON st.station_key = CONCAT('openaq:', s.location_id)
        WHERE s.item_id = @item_id AND TRY_CONVERT(INT, s.sensor_id) IS NOT NULL
    ) AS s ON t.sensor_id = s.sensor_id
    WHEN MATCHED AND (t.station_id <> s.station_id OR t.pollutant <> s.pollutant OR ISNULL(t.units, '') <> ISNULL(s.units, '')) THEN
        UPDATE SET station_id = s.station_id, pollutant = s.pollutant, units = s.units
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (sensor_id, station_id, pollutant, units) VALUES (s.sensor_id, s.station_id, s.pollutant, s.units);

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DELETE FROM stg.openaq_locations WHERE item_id = @item_id;
    EXEC etl.usp_log_merge @run_id, 'openaq_locations', @item_id, @started, @read, @inserted, @updated, 0;
END;
GO

CREATE OR ALTER PROCEDURE core.usp_merge_aqhi_stations
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.aqhi_stations WHERE item_id = @item_id);

    WITH src AS (
        SELECT location_id, name_en AS name, zone,
               TRY_CONVERT(DECIMAL(9, 6), lat) AS lat, TRY_CONVERT(DECIMAL(9, 6), lon) AS lon,
               HASHBYTES('SHA2_256', CONCAT(name_en, '|', zone, '|', lat, '|', lon)) AS row_hash
        FROM stg.aqhi_stations
        WHERE item_id = @item_id
    )
    MERGE core.station AS t
    USING src AS s ON t.station_key = CONCAT('aqhi:', s.location_id)
    WHEN MATCHED AND ISNULL(t.row_hash, 0x) <> s.row_hash THEN
        UPDATE SET name = s.name, city = s.zone, lat = s.lat, lon = s.lon, row_hash = s.row_hash,
                   last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (station_key, source, source_ref, name, city, lat, lon, provider, licence, attribution, is_monitor,
                is_low_cost_sensor, row_hash, first_seen_run, last_changed_run)
        VALUES (CONCAT('aqhi:', s.location_id), 'aqhi', s.location_id, s.name, s.zone, s.lat, s.lon,
                N'Environment and Climate Change Canada (AQHI)', N'Open Government Licence - Canada',
                N'Environment and Climate Change Canada', 1, 0, s.row_hash, @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DELETE FROM stg.aqhi_stations WHERE item_id = @item_id;
    EXEC etl.usp_log_merge @run_id, 'aqhi_stations', @item_id, @started, @read, @inserted, @updated, 0;
END;
GO

CREATE OR ALTER PROCEDURE core.usp_merge_climate_stations
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.climate_stations WHERE item_id = @item_id);

    WITH src AS (
        SELECT CAST(climate_id AS VARCHAR(20)) AS climate_id, TRY_CONVERT(INT, stn_id) AS stn_id,
               station_name AS name, TRY_CONVERT(DECIMAL(9, 6), lat) AS lat, TRY_CONVERT(DECIMAL(9, 6), lon) AS lon,
               TRY_CONVERT(DECIMAL(8, 1), NULLIF(elevation_m, '')) AS elevation_m,
               TRY_CONVERT(DATETIME2(0), NULLIF(hly_first, '')) AS hly_first,
               TRY_CONVERT(DATETIME2(0), NULLIF(hly_last, '')) AS hly_last,
               HASHBYTES('SHA2_256', CONCAT(stn_id, '|', station_name, '|', lat, '|', lon, '|', elevation_m, '|',
                                            hly_first, '|', hly_last)) AS row_hash
        FROM stg.climate_stations
        WHERE item_id = @item_id
    )
    MERGE core.climate_station AS t
    USING src AS s ON t.climate_id = s.climate_id
    WHEN MATCHED AND ISNULL(t.row_hash, 0x) <> s.row_hash THEN
        UPDATE SET stn_id = s.stn_id, name = s.name, lat = s.lat, lon = s.lon, elevation_m = s.elevation_m,
                   hly_first = s.hly_first, hly_last = s.hly_last, row_hash = s.row_hash, last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (climate_id, stn_id, name, lat, lon, elevation_m, hly_first, hly_last, row_hash, first_seen_run, last_changed_run)
        VALUES (s.climate_id, s.stn_id, s.name, s.lat, s.lon, s.elevation_m, s.hly_first, s.hly_last, s.row_hash,
                @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DELETE FROM stg.climate_stations WHERE item_id = @item_id;
    EXEC etl.usp_log_merge @run_id, 'climate_stations', @item_id, @started, @read, @inserted, @updated, 0;
END;
GO

-------------------------------------------------------------------------------------------------------------
-- weather: observed (eccc climate stations) and forecast (open-meteo)
-------------------------------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE core.usp_merge_climate_hourly
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.climate_hourly WHERE item_id = @item_id);

    WITH typed AS (
        SELECT CAST(climate_id AS VARCHAR(20)) AS climate_id,
               TRY_CONVERT(DATETIME2(0), LEFT(hour_utc, 19), 126) AS hour_utc,
               TRY_CONVERT(DECIMAL(5, 1), TRY_CONVERT(FLOAT, NULLIF(temp_c, ''))) AS temp_c,
               TRY_CONVERT(DECIMAL(5, 1), TRY_CONVERT(FLOAT, NULLIF(rh_pct, ''))) AS rh_pct,
               TRY_CONVERT(DECIMAL(6, 1), TRY_CONVERT(FLOAT, NULLIF(wind_kmh, ''))) AS wind_kmh,
               TRY_CONVERT(SMALLINT, TRY_CONVERT(FLOAT, NULLIF(wind_dir_deg, ''))) AS wind_dir_deg,
               TRY_CONVERT(DECIMAL(6, 2), TRY_CONVERT(FLOAT, NULLIF(pressure_kpa, ''))) AS pressure_kpa,
               TRY_CONVERT(DECIMAL(7, 2), TRY_CONVERT(FLOAT, NULLIF(precip_mm, ''))) AS precip_mm,
               NULLIF(flags, '') AS flags
        FROM stg.climate_hourly
        WHERE item_id = @item_id
    ),
    ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY climate_id, hour_utc ORDER BY temp_c DESC) AS rn
        FROM typed WHERE hour_utc IS NOT NULL
    ),
    src AS (
        SELECT climate_id, hour_utc, temp_c, rh_pct, wind_kmh, wind_dir_deg, pressure_kpa, precip_mm, flags,
               HASHBYTES('SHA2_256', CONCAT(temp_c, '|', rh_pct, '|', wind_kmh, '|', wind_dir_deg, '|', pressure_kpa, '|',
                                            precip_mm, '|', flags)) AS row_hash
        FROM ranked WHERE rn = 1
    )
    MERGE core.weather_hourly WITH (HOLDLOCK) AS t
    USING src AS s ON t.climate_id = s.climate_id AND t.hour_utc = s.hour_utc
    WHEN MATCHED AND t.row_hash <> s.row_hash THEN
        UPDATE SET temp_c = s.temp_c, rh_pct = s.rh_pct, wind_kmh = s.wind_kmh, wind_dir_deg = s.wind_dir_deg,
                   pressure_kpa = s.pressure_kpa, precip_mm = s.precip_mm, flags = s.flags, row_hash = s.row_hash,
                   last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (climate_id, hour_utc, temp_c, rh_pct, wind_kmh, wind_dir_deg, pressure_kpa, precip_mm, flags,
                row_hash, first_seen_run, last_changed_run)
        VALUES (s.climate_id, s.hour_utc, s.temp_c, s.rh_pct, s.wind_kmh, s.wind_dir_deg, s.pressure_kpa,
                s.precip_mm, s.flags, s.row_hash, @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DECLARE @valid BIGINT = (SELECT COUNT(*) FROM stg.climate_hourly
                             WHERE item_id = @item_id AND TRY_CONVERT(DATETIME2(0), LEFT(hour_utc, 19), 126) IS NOT NULL);
    DELETE FROM stg.climate_hourly WHERE item_id = @item_id;
    DECLARE @rejected BIGINT = @read - @valid;
    EXEC etl.usp_log_merge @run_id, 'climate_hourly', @item_id, @started, @read, @inserted, @updated, @rejected;
END;
GO

CREATE OR ALTER PROCEDURE core.usp_merge_weather_forecast
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.weather_forecast WHERE item_id = @item_id);
    DECLARE @source_id VARCHAR(40) = (SELECT TOP (1) CONCAT('openmeteo_', IIF(kind = 'live', 'live', 'hist'))
                                      FROM stg.weather_forecast WHERE item_id = @item_id);

    MERGE core.weather_point AS t
    USING (SELECT DISTINCT CAST(point_id AS VARCHAR(30)) AS point_id, TRY_CONVERT(DECIMAL(9, 4), lat) AS lat,
                           TRY_CONVERT(DECIMAL(9, 4), lon) AS lon
           FROM stg.weather_forecast WHERE item_id = @item_id) AS s
        ON t.point_id = s.point_id
    WHEN NOT MATCHED BY TARGET THEN INSERT (point_id, lat, lon) VALUES (s.point_id, s.lat, s.lon);

    WITH typed AS (
        SELECT CAST(point_id AS VARCHAR(30)) AS point_id,
               TRY_CONVERT(DATETIME2(0), LEFT(hour_utc, 19), 126) AS hour_utc,
               CAST(kind AS VARCHAR(10)) AS kind,
               TRY_CONVERT(DECIMAL(5, 1), TRY_CONVERT(FLOAT, NULLIF(temp_c, ''))) AS temp_c,
               TRY_CONVERT(DECIMAL(5, 1), TRY_CONVERT(FLOAT, NULLIF(rh_pct, ''))) AS rh_pct,
               TRY_CONVERT(DECIMAL(6, 1), TRY_CONVERT(FLOAT, NULLIF(wind_kmh, ''))) AS wind_kmh,
               TRY_CONVERT(SMALLINT, TRY_CONVERT(FLOAT, NULLIF(wind_dir_deg, ''))) AS wind_dir_deg,
               TRY_CONVERT(DECIMAL(8, 1), TRY_CONVERT(FLOAT, NULLIF(blh_m, ''))) AS blh_m,
               TRY_CONVERT(DECIMAL(7, 2), TRY_CONVERT(FLOAT, NULLIF(precip_mm, ''))) AS precip_mm,
               TRY_CONVERT(DECIMAL(5, 1), TRY_CONVERT(FLOAT, NULLIF(cloud_pct, ''))) AS cloud_pct
        FROM stg.weather_forecast
        WHERE item_id = @item_id
    ),
    src AS (
        SELECT *, HASHBYTES('SHA2_256', CONCAT(kind, '|', temp_c, '|', rh_pct, '|', wind_kmh, '|', wind_dir_deg, '|',
                                               blh_m, '|', precip_mm, '|', cloud_pct)) AS row_hash
        FROM typed WHERE hour_utc IS NOT NULL
    )
    MERGE core.weather_forecast_hourly WITH (HOLDLOCK) AS t
    USING src AS s ON t.point_id = s.point_id AND t.hour_utc = s.hour_utc
    -- a live forecast never overwrites an archived (hist) one: training and the archive stay consistent
    WHEN MATCHED AND t.row_hash <> s.row_hash AND NOT (t.kind = 'hist' AND s.kind = 'live') THEN
        UPDATE SET kind = s.kind, temp_c = s.temp_c, rh_pct = s.rh_pct, wind_kmh = s.wind_kmh,
                   wind_dir_deg = s.wind_dir_deg, blh_m = s.blh_m, precip_mm = s.precip_mm, cloud_pct = s.cloud_pct,
                   row_hash = s.row_hash, last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (point_id, hour_utc, kind, temp_c, rh_pct, wind_kmh, wind_dir_deg, blh_m, precip_mm, cloud_pct,
                row_hash, first_seen_run, last_changed_run)
        VALUES (s.point_id, s.hour_utc, s.kind, s.temp_c, s.rh_pct, s.wind_kmh, s.wind_dir_deg, s.blh_m, s.precip_mm,
                s.cloud_pct, s.row_hash, @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DECLARE @valid BIGINT = (SELECT COUNT(*) FROM stg.weather_forecast
                             WHERE item_id = @item_id AND TRY_CONVERT(DATETIME2(0), LEFT(hour_utc, 19), 126) IS NOT NULL);
    DELETE FROM stg.weather_forecast WHERE item_id = @item_id;
    DECLARE @rejected BIGINT = @read - @valid;
    EXEC etl.usp_log_merge @run_id, @source_id, @item_id, @started, @read, @inserted, @updated, @rejected;
END;
GO

-------------------------------------------------------------------------------------------------------------
-- official aqhi (observations and forecasts)
-------------------------------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE core.usp_merge_aqhi_obs
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.aqhi_obs WHERE item_id = @item_id);

    WITH typed AS (
        SELECT CAST(location_id AS VARCHAR(20)) AS location_id,
               TRY_CONVERT(DATETIME2(0), LEFT(observation_utc, 19), 126) AS observation_utc,
               TRY_CONVERT(DECIMAL(5, 2), TRY_CONVERT(FLOAT, NULLIF(aqhi, ''))) AS aqhi
        FROM stg.aqhi_obs WHERE item_id = @item_id
    ),
    src AS (
        SELECT DISTINCT location_id, observation_utc, aqhi, HASHBYTES('SHA2_256', CONCAT(aqhi, '')) AS row_hash
        FROM typed WHERE observation_utc IS NOT NULL
    )
    MERGE core.aqhi_obs AS t
    USING src AS s ON t.location_id = s.location_id AND t.observation_utc = s.observation_utc
    WHEN MATCHED AND t.row_hash <> s.row_hash THEN
        UPDATE SET aqhi = s.aqhi, row_hash = s.row_hash, last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (location_id, observation_utc, aqhi, row_hash, first_seen_run, last_changed_run)
        VALUES (s.location_id, s.observation_utc, s.aqhi, s.row_hash, @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DELETE FROM stg.aqhi_obs WHERE item_id = @item_id;
    EXEC etl.usp_log_merge @run_id, 'aqhi_obs', @item_id, @started, @read, @inserted, @updated, 0;
END;
GO

CREATE OR ALTER PROCEDURE core.usp_merge_aqhi_forecast
    @run_id INT, @item_id NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @actions TABLE (act NVARCHAR(10));
    DECLARE @read BIGINT = (SELECT COUNT(*) FROM stg.aqhi_forecast WHERE item_id = @item_id);

    WITH typed AS (
        SELECT CAST(location_id AS VARCHAR(20)) AS location_id,
               TRY_CONVERT(DATETIME2(0), LEFT(publication_utc, 19), 126) AS publication_utc,
               CAST(kind AS VARCHAR(10)) AS kind,
               CAST(slot AS VARCHAR(30)) AS slot,
               TRY_CONVERT(DATETIME2(0), LEFT(NULLIF(forecast_utc, ''), 19), 126) AS forecast_utc,
               NULLIF(period_label, '') AS period_label,
               TRY_CONVERT(DECIMAL(5, 2), TRY_CONVERT(FLOAT, NULLIF(aqhi, ''))) AS aqhi
        FROM stg.aqhi_forecast WHERE item_id = @item_id
    ),
    ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY location_id, publication_utc, kind, slot ORDER BY aqhi DESC) AS rn
        FROM typed WHERE publication_utc IS NOT NULL
    ),
    src AS (
        SELECT location_id, publication_utc, kind, slot, forecast_utc, period_label, aqhi,
               HASHBYTES('SHA2_256', CONCAT(forecast_utc, '|', period_label, '|', aqhi)) AS row_hash
        FROM ranked WHERE rn = 1
    )
    MERGE core.aqhi_forecast AS t
    USING src AS s
        ON t.location_id = s.location_id AND t.publication_utc = s.publication_utc AND t.kind = s.kind AND t.slot = s.slot
    WHEN MATCHED AND t.row_hash <> s.row_hash THEN
        UPDATE SET forecast_utc = s.forecast_utc, period_label = s.period_label, aqhi = s.aqhi, row_hash = s.row_hash,
                   last_changed_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (location_id, publication_utc, kind, slot, forecast_utc, period_label, aqhi, row_hash,
                first_seen_run, last_changed_run)
        VALUES (s.location_id, s.publication_utc, s.kind, s.slot, s.forecast_utc, s.period_label, s.aqhi, s.row_hash,
                @run_id, @run_id)
    OUTPUT $action INTO @actions;

    DECLARE @inserted BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'INSERT');
    DECLARE @updated BIGINT = (SELECT COUNT(*) FROM @actions WHERE act = 'UPDATE');
    DELETE FROM stg.aqhi_forecast WHERE item_id = @item_id;
    EXEC etl.usp_log_merge @run_id, 'aqhi_forecast', @item_id, @started, @read, @inserted, @updated, 0;
END;
GO
