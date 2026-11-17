-- 10_tables.sql
-- every table. stg tables are all text, one column per landing csv column, so a bulk load never fails on
-- types; the merge procs do the typing with TRY_CONVERT. core tables are typed and carry row_hash plus the
-- runs that first saw and last changed each row. idempotent: only creates what's missing.

-------------------------------------------------------------------------------------------------------------
-- stg: one table per landing csv shape. item_id lets parallel loads of the same source share a table.
-------------------------------------------------------------------------------------------------------------

IF OBJECT_ID('stg.naps_stations') IS NULL
CREATE TABLE stg.naps_stations (
    item_id NVARCHAR(200), naps_id NVARCHAR(20), station_name NVARCHAR(200), status NVARCHAR(10),
    city NVARCHAR(100), province NVARCHAR(100), utc_offset_h NVARCHAR(10), lat NVARCHAR(30), lon NVARCHAR(30),
    elevation_m NVARCHAR(30), start_date NVARCHAR(30), end_date NVARCHAR(30), site_type NVARCHAR(20),
    urbanization NVARCHAR(20)
);
GO

-- naps_hourly and openaq_hours land in the same csv shape, so they share one staging table
IF OBJECT_ID('stg.measurement') IS NULL
CREATE TABLE stg.measurement (
    item_id NVARCHAR(200), source NVARCHAR(20), station_ref NVARCHAR(50), sensor_id NVARCHAR(30),
    pollutant NVARCHAR(10), hour_utc NVARCHAR(30), value NVARCHAR(30), unit NVARCHAR(20),
    method_code NVARCHAR(20), coverage_pct NVARCHAR(30), qc_flag NVARCHAR(50)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_stg_measurement_item')
    CREATE INDEX ix_stg_measurement_item ON stg.measurement (item_id);
GO

IF OBJECT_ID('stg.openaq_locations') IS NULL
CREATE TABLE stg.openaq_locations (
    item_id NVARCHAR(200), sensor_id NVARCHAR(30), location_id NVARCHAR(30), location_name NVARCHAR(200),
    locality NVARCHAR(200), country NVARCHAR(10), provider NVARCHAR(100), owner NVARCHAR(200),
    is_monitor NVARCHAR(5), is_mobile NVARCHAR(5), lat NVARCHAR(30), lon NVARCHAR(30), timezone NVARCHAR(60),
    pollutant NVARCHAR(10), units NVARCHAR(20), licences NVARCHAR(400), attribution NVARCHAR(400),
    first_utc NVARCHAR(30), last_utc NVARCHAR(30)
);
GO

IF OBJECT_ID('stg.aqhi_stations') IS NULL
CREATE TABLE stg.aqhi_stations (
    item_id NVARCHAR(200), location_id NVARCHAR(20), name_en NVARCHAR(200), lat NVARCHAR(30), lon NVARCHAR(30),
    zone NVARCHAR(200), naps_ids NVARCHAR(200)
);
GO

IF OBJECT_ID('stg.aqhi_obs') IS NULL
CREATE TABLE stg.aqhi_obs (
    item_id NVARCHAR(200), location_id NVARCHAR(20), observation_utc NVARCHAR(30), aqhi NVARCHAR(30)
);
GO

IF OBJECT_ID('stg.aqhi_forecast') IS NULL
CREATE TABLE stg.aqhi_forecast (
    item_id NVARCHAR(200), location_id NVARCHAR(20), publication_utc NVARCHAR(30), kind NVARCHAR(10),
    slot NVARCHAR(30), forecast_utc NVARCHAR(30), period_label NVARCHAR(60), aqhi NVARCHAR(30)
);
GO

IF OBJECT_ID('stg.climate_stations') IS NULL
CREATE TABLE stg.climate_stations (
    item_id NVARCHAR(200), climate_id NVARCHAR(20), stn_id NVARCHAR(20), station_name NVARCHAR(200),
    lat NVARCHAR(30), lon NVARCHAR(30), elevation_m NVARCHAR(30), hly_first NVARCHAR(30), hly_last NVARCHAR(30)
);
GO

IF OBJECT_ID('stg.climate_hourly') IS NULL
CREATE TABLE stg.climate_hourly (
    item_id NVARCHAR(200), climate_id NVARCHAR(20), hour_utc NVARCHAR(30), temp_c NVARCHAR(30),
    rh_pct NVARCHAR(30), wind_kmh NVARCHAR(30), wind_dir_deg NVARCHAR(30), pressure_kpa NVARCHAR(30),
    precip_mm NVARCHAR(30), flags NVARCHAR(400)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_stg_climate_hourly_item')
    CREATE INDEX ix_stg_climate_hourly_item ON stg.climate_hourly (item_id);
GO

-- openmeteo_hist and openmeteo_live share this table; kind says which
IF OBJECT_ID('stg.weather_forecast') IS NULL
CREATE TABLE stg.weather_forecast (
    item_id NVARCHAR(200), point_id NVARCHAR(30), lat NVARCHAR(30), lon NVARCHAR(30), hour_utc NVARCHAR(30),
    kind NVARCHAR(10), temp_c NVARCHAR(30), rh_pct NVARCHAR(30), wind_kmh NVARCHAR(30),
    wind_dir_deg NVARCHAR(30), blh_m NVARCHAR(30), precip_mm NVARCHAR(30), cloud_pct NVARCHAR(30)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_stg_weather_forecast_item')
    CREATE INDEX ix_stg_weather_forecast_item ON stg.weather_forecast (item_id);
GO

-------------------------------------------------------------------------------------------------------------
-- etl: run log, per-item stage log, watermarks, data quality results
-------------------------------------------------------------------------------------------------------------

IF OBJECT_ID('etl.pipeline_run') IS NULL
CREATE TABLE etl.pipeline_run (
    run_id INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_pipeline_run PRIMARY KEY,
    mode VARCHAR(12) NOT NULL,                  -- full or incremental
    target VARCHAR(10) NOT NULL,                -- local or azure
    trigger_name NVARCHAR(100) NULL,
    status VARCHAR(12) NOT NULL,                -- running, succeeded, failed
    started_at DATETIME2(3) NOT NULL,
    ended_at DATETIME2(3) NULL,
    duration_s AS DATEDIFF_BIG(MILLISECOND, started_at, ended_at) / 1000.0,
    items_planned INT NULL,
    rows_read BIGINT NULL,
    rows_written BIGINT NULL,
    rows_inserted BIGINT NULL,
    rows_updated BIGINT NULL,
    measurement_rows BIGINT NULL,               -- core.measurement size at the end, for the drop check
    error_message NVARCHAR(4000) NULL
);
GO

IF OBJECT_ID('etl.stage_run') IS NULL
CREATE TABLE etl.stage_run (
    stage_run_id BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_stage_run PRIMARY KEY,
    run_id INT NOT NULL,
    stage VARCHAR(20) NOT NULL,                 -- plan, extract, load, merge, link, match, dq, mart, model, publish
    source_id VARCHAR(40) NULL,
    item_id NVARCHAR(200) NULL,
    status VARCHAR(12) NOT NULL,
    started_at DATETIME2(3) NULL,
    ended_at DATETIME2(3) NULL,
    seconds DECIMAL(12, 3) NULL,
    rows_read BIGINT NULL,
    rows_written BIGINT NULL,
    rows_inserted BIGINT NULL,
    rows_updated BIGINT NULL,
    rows_rejected BIGINT NULL,
    message NVARCHAR(4000) NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_stage_run_run')
    CREATE INDEX ix_stage_run_run ON etl.stage_run (run_id, stage);
GO

IF OBJECT_ID('etl.source_state') IS NULL
CREATE TABLE etl.source_state (
    source_id VARCHAR(40) NOT NULL,
    state_key NVARCHAR(400) NOT NULL,
    value_text NVARCHAR(400) NULL,
    value_utc DATETIME2(0) NULL,
    updated_run INT NOT NULL,
    updated_at DATETIME2(3) NOT NULL,
    CONSTRAINT pk_source_state PRIMARY KEY (source_id, state_key)
);
GO

IF OBJECT_ID('etl.sensor_state') IS NULL
CREATE TABLE etl.sensor_state (
    sensor_id INT NOT NULL CONSTRAINT pk_sensor_state PRIMARY KEY,
    last_hour_utc DATETIME2(0) NOT NULL,
    updated_run INT NOT NULL,
    updated_at DATETIME2(3) NOT NULL
);
GO

IF OBJECT_ID('etl.dq_result') IS NULL
CREATE TABLE etl.dq_result (
    dq_result_id BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_dq_result PRIMARY KEY,
    run_id INT NOT NULL,
    check_name VARCHAR(60) NOT NULL,
    severity VARCHAR(10) NOT NULL,              -- error, warn, info
    status VARCHAR(10) NOT NULL,                -- pass, warn, fail
    observed DECIMAL(18, 4) NULL,
    threshold DECIMAL(18, 4) NULL,
    detail NVARCHAR(1000) NULL,
    checked_at DATETIME2(3) NOT NULL
);
GO

-------------------------------------------------------------------------------------------------------------
-- core: stations, links, measurements, weather, official aqhi
-------------------------------------------------------------------------------------------------------------

-- one row per station per source. station_xref folds rows that are the same physical site into one site_id.
IF OBJECT_ID('core.station') IS NULL
CREATE TABLE core.station (
    station_id INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_station PRIMARY KEY,
    station_key VARCHAR(60) NOT NULL CONSTRAINT uq_station_key UNIQUE,   -- naps:060410, openaq:1274950, aqhi:FEVNS
    source VARCHAR(10) NOT NULL,                -- naps, openaq, aqhi
    source_ref VARCHAR(40) NOT NULL,
    name NVARCHAR(200) NULL,
    city NVARCHAR(100) NULL,
    lat DECIMAL(9, 6) NULL,
    lon DECIMAL(9, 6) NULL,
    elevation_m DECIMAL(8, 1) NULL,
    site_type VARCHAR(20) NULL,
    urbanization VARCHAR(20) NULL,
    utc_offset_h DECIMAL(4, 1) NULL,
    provider NVARCHAR(100) NULL,
    licence NVARCHAR(400) NULL,
    attribution NVARCHAR(400) NULL,
    is_monitor BIT NOT NULL CONSTRAINT df_station_monitor DEFAULT 1,
    is_low_cost_sensor BIT NOT NULL CONSTRAINT df_station_low_cost DEFAULT 0,
    first_utc DATETIME2(0) NULL,
    last_utc DATETIME2(0) NULL,
    row_hash VARBINARY(32) NULL,
    first_seen_run INT NULL,
    last_changed_run INT NULL
);
GO

IF OBJECT_ID('core.station_xref') IS NULL
CREATE TABLE core.station_xref (
    station_id INT NOT NULL CONSTRAINT pk_station_xref PRIMARY KEY,
    site_id INT NOT NULL,                       -- station_id of the site's preferred row (naps > openaq > aqhi)
    linked_to INT NULL,                         -- the station this one was matched to, null for site heads
    link_method VARCHAR(30) NOT NULL,           -- self, distance_name, aqhi_name
    distance_m DECIMAL(10, 1) NULL,
    linked_run INT NULL
);
GO

IF OBJECT_ID('core.openaq_sensor') IS NULL
CREATE TABLE core.openaq_sensor (
    sensor_id INT NOT NULL CONSTRAINT pk_openaq_sensor PRIMARY KEY,
    station_id INT NOT NULL,
    pollutant VARCHAR(10) NOT NULL,
    units VARCHAR(20) NULL
);
GO

IF OBJECT_ID('core.measurement') IS NULL
CREATE TABLE core.measurement (
    station_id INT NOT NULL,
    pollutant VARCHAR(10) NOT NULL,
    hour_utc DATETIME2(0) NOT NULL,             -- start of the hour, utc
    source VARCHAR(10) NOT NULL,
    value DECIMAL(10, 3) NULL,                  -- ug/m3 for pm, ppb for o3/no2
    coverage_pct DECIMAL(6, 2) NULL,
    method_code VARCHAR(20) NULL,
    sensor_id INT NULL,
    qc_flag VARCHAR(50) NULL,
    row_hash VARBINARY(32) NOT NULL,
    first_seen_run INT NOT NULL,
    last_changed_run INT NOT NULL,
    CONSTRAINT pk_measurement PRIMARY KEY (station_id, pollutant, hour_utc, source)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_measurement_hour')
    CREATE INDEX ix_measurement_hour ON core.measurement (hour_utc) INCLUDE (station_id, pollutant, value);
GO
-- lets mart.usp_refresh find the earliest hour a run changed without scanning the whole table
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_measurement_changed')
    CREATE INDEX ix_measurement_changed ON core.measurement (last_changed_run) INCLUDE (hour_utc);
GO

IF OBJECT_ID('core.climate_station') IS NULL
CREATE TABLE core.climate_station (
    climate_id VARCHAR(20) NOT NULL CONSTRAINT pk_climate_station PRIMARY KEY,
    stn_id INT NULL,
    name NVARCHAR(200) NULL,
    lat DECIMAL(9, 6) NULL,
    lon DECIMAL(9, 6) NULL,
    elevation_m DECIMAL(8, 1) NULL,
    hly_first DATETIME2(0) NULL,
    hly_last DATETIME2(0) NULL,
    row_hash VARBINARY(32) NULL,
    first_seen_run INT NULL,
    last_changed_run INT NULL
);
GO

IF OBJECT_ID('core.weather_hourly') IS NULL
CREATE TABLE core.weather_hourly (
    climate_id VARCHAR(20) NOT NULL,
    hour_utc DATETIME2(0) NOT NULL,
    temp_c DECIMAL(5, 1) NULL,
    rh_pct DECIMAL(5, 1) NULL,
    wind_kmh DECIMAL(6, 1) NULL,
    wind_dir_deg SMALLINT NULL,
    pressure_kpa DECIMAL(6, 2) NULL,
    precip_mm DECIMAL(7, 2) NULL,
    flags NVARCHAR(400) NULL,
    row_hash VARBINARY(32) NOT NULL,
    first_seen_run INT NOT NULL,
    last_changed_run INT NOT NULL,
    CONSTRAINT pk_weather_hourly PRIMARY KEY (climate_id, hour_utc)
);
GO

IF OBJECT_ID('core.weather_point') IS NULL
CREATE TABLE core.weather_point (
    point_id VARCHAR(30) NOT NULL CONSTRAINT pk_weather_point PRIMARY KEY,
    lat DECIMAL(9, 4) NOT NULL,
    lon DECIMAL(9, 4) NOT NULL
);
GO

IF OBJECT_ID('core.weather_forecast_hourly') IS NULL
CREATE TABLE core.weather_forecast_hourly (
    point_id VARCHAR(30) NOT NULL,
    hour_utc DATETIME2(0) NOT NULL,
    kind VARCHAR(10) NOT NULL,                  -- hist (archived forecast) or live (latest run)
    temp_c DECIMAL(5, 1) NULL,
    rh_pct DECIMAL(5, 1) NULL,
    wind_kmh DECIMAL(6, 1) NULL,
    wind_dir_deg SMALLINT NULL,
    blh_m DECIMAL(8, 1) NULL,
    precip_mm DECIMAL(7, 2) NULL,
    cloud_pct DECIMAL(5, 1) NULL,
    row_hash VARBINARY(32) NOT NULL,
    first_seen_run INT NOT NULL,
    last_changed_run INT NOT NULL,
    CONSTRAINT pk_weather_forecast_hourly PRIMARY KEY (point_id, hour_utc)
);
GO

IF OBJECT_ID('core.station_weather_match') IS NULL
CREATE TABLE core.station_weather_match (
    station_id INT NOT NULL CONSTRAINT pk_station_weather_match PRIMARY KEY,
    climate_id VARCHAR(20) NULL,
    climate_distance_km DECIMAL(8, 3) NULL,
    is_used BIT NOT NULL,                       -- 0 when the nearest climate station is over MAX_MATCH_KM
    point_id VARCHAR(30) NULL,
    point_distance_km DECIMAL(8, 3) NULL,
    matched_run INT NULL
);
GO

-- the official aqhi archive. eccc only serves ~3 days, so these two are never truncated, not even in full mode.
IF OBJECT_ID('core.aqhi_obs') IS NULL
CREATE TABLE core.aqhi_obs (
    location_id VARCHAR(20) NOT NULL,
    observation_utc DATETIME2(0) NOT NULL,      -- end of the last averaged hour (eccc's convention)
    aqhi DECIMAL(5, 2) NULL,
    row_hash VARBINARY(32) NOT NULL,
    first_seen_run INT NOT NULL,
    last_changed_run INT NOT NULL,
    CONSTRAINT pk_aqhi_obs PRIMARY KEY (location_id, observation_utc)
);
GO

IF OBJECT_ID('core.aqhi_forecast') IS NULL
CREATE TABLE core.aqhi_forecast (
    location_id VARCHAR(20) NOT NULL,
    publication_utc DATETIME2(0) NOT NULL,
    kind VARCHAR(10) NOT NULL,                  -- hourly or period
    slot VARCHAR(30) NOT NULL,                  -- forecast hour (hourly) or period_n (period)
    forecast_utc DATETIME2(0) NULL,
    period_label NVARCHAR(60) NULL,             -- Today, Tonight, Tomorrow, ...
    aqhi DECIMAL(5, 2) NULL,
    row_hash VARBINARY(32) NOT NULL,
    first_seen_run INT NOT NULL,
    last_changed_run INT NOT NULL,
    CONSTRAINT pk_aqhi_forecast PRIMARY KEY (location_id, publication_utc, kind, slot)
);
GO

-------------------------------------------------------------------------------------------------------------
-- mart: one row per site per local day, and the forecasts
-------------------------------------------------------------------------------------------------------------

-- "to6" columns cover the 24 h (or 6 h) up to 06:00 local on that date: what's known when a forecast is issued.
-- the plain day columns cover the whole local day and are only used as targets or for earlier days.
IF OBJECT_ID('mart.daily_station') IS NULL
CREATE TABLE mart.daily_station (
    site_id INT NOT NULL,
    local_date DATE NOT NULL,
    pm25_mean DECIMAL(8, 3) NULL,
    pm25_max DECIMAL(8, 3) NULL,
    pm25_hours TINYINT NULL,
    pm25_completeness DECIMAL(5, 2) NULL,
    o3_8h_max DECIMAL(8, 3) NULL,
    no2_mean DECIMAL(8, 3) NULL,
    aqhi_max DECIMAL(5, 2) NULL,
    pm25_24h_to6_mean DECIMAL(8, 3) NULL,
    pm25_24h_to6_max DECIMAL(8, 3) NULL,
    pm25_24h_to6_hours TINYINT NULL,
    pm25_6h_to6_mean DECIMAL(8, 3) NULL,
    o3_8h_to6_max DECIMAL(8, 3) NULL,
    no2_24h_to6_mean DECIMAL(8, 3) NULL,
    obs_temp_mean DECIMAL(5, 1) NULL,
    obs_temp_max DECIMAL(5, 1) NULL,
    obs_wind_mean DECIMAL(6, 1) NULL,
    obs_rh_mean DECIMAL(5, 1) NULL,
    obs_precip_sum DECIMAL(7, 2) NULL,
    fc_temp_mean DECIMAL(5, 1) NULL,
    fc_temp_max DECIMAL(5, 1) NULL,
    fc_wind_mean DECIMAL(6, 1) NULL,
    fc_wind_sin DECIMAL(6, 4) NULL,
    fc_wind_cos DECIMAL(6, 4) NULL,
    fc_rh_mean DECIMAL(5, 1) NULL,
    fc_precip_sum DECIMAL(7, 2) NULL,
    fc_cloud_mean DECIMAL(5, 1) NULL,
    fc_blh_min DECIMAL(8, 1) NULL,
    fc_kind VARCHAR(10) NULL,                   -- hist or live: where the forecast weather came from
    updated_run INT NOT NULL,
    CONSTRAINT pk_daily_station PRIMARY KEY (site_id, local_date)
);
GO

IF OBJECT_ID('mart.forecast') IS NULL
CREATE TABLE mart.forecast (
    site_id INT NOT NULL,
    target_date DATE NOT NULL,
    issued_at DATETIME2(0) NOT NULL,
    pm25_pred DECIMAL(8, 3) NOT NULL,
    pm25_p10 DECIMAL(8, 3) NULL,
    pm25_p90 DECIMAL(8, 3) NULL,
    aqhi_pred DECIMAL(5, 2) NULL,
    aqhi_category VARCHAR(12) NULL,
    model_version VARCHAR(40) NOT NULL,
    run_id INT NULL,
    CONSTRAINT pk_forecast PRIMARY KEY (site_id, target_date, model_version)
);
GO
