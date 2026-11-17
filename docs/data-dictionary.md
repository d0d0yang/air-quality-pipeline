# Data dictionary

Every table the pipeline writes, by schema. Times are UTC unless a column says local. Hours are stored as the
**start** of the hour (`hour_utc`); ECCC's AQHI observations keep ECCC's own convention (end of the hour).
DDL: [sql/10_tables.sql](../sql/10_tables.sql).

## stg (landing, all text)

One table per landing CSV shape. Every column is `NVARCHAR` so a bulk load never fails on types; the merge procs
type everything with `TRY_CONVERT` and count what doesn't convert. `item_id` says which work item loaded the row,
so parallel loads of the same source never collide. Rows are deleted once their item is merged.

| Table | Filled by | Notes |
|---|---|---|
| `stg.measurement` | `naps_hourly`, `openaq_hours` | one row per station, pollutant and hour |
| `stg.naps_stations` | `naps_stations` | NAPS station list, region only |
| `stg.openaq_locations` | `openaq_locations` | one row per sensor, with its location's provider and licence |
| `stg.aqhi_stations`, `stg.aqhi_obs`, `stg.aqhi_forecast` | GeoMet AQHI collections | |
| `stg.climate_stations`, `stg.climate_hourly` | GeoMet climate collections | |
| `stg.weather_forecast` | `openmeteo_hist`, `openmeteo_live` | `kind` = hist or live |

## core (clean, typed)

All fact tables carry `row_hash` (SHA-256 of the non-key columns), `first_seen_run` and `last_changed_run`.
A merge only updates a row when its hash changed.

### core.station
One row per station per source (`station_key` like `naps:060410`, `openaq:1274950`, `aqhi:FEVNS`).

| Column | Meaning |
|---|---|
| `station_id` | surrogate key, stable across full reloads (stations are merged, never truncated) |
| `source` | naps, openaq or aqhi |
| `lat`, `lon`, `elevation_m` | WGS84 coordinates; elevation from the NAPS list |
| `site_type`, `urbanization` | NAPS codes (e.g. urbanization LU = large urban, MU, SU, NU = non-urban) |
| `utc_offset_h` | NAPS standard-time offset (−5 in this region), used to convert the NAPS files |
| `provider`, `licence`, `attribution` | shown on the dashboard footer |
| `is_monitor`, `is_low_cost_sensor` | OpenAQ's `isMonitor`; low-cost sensors are flagged and kept out of training |

### core.station_xref
Folds stations that are the same physical site into one `site_id` (the station that heads the site).

| `link_method` | Rule |
|---|---|
| `self` | heads its own site (every NAPS station, every low-cost sensor, unmatched stations) |
| `distance_name` | OpenAQ government monitor within 250 m of a NAPS station with the same site name |
| `openaq_duplicate` | OpenAQ re-registered the same site under a new id (same name, within 250 m) |
| `aqhi_name` | ECCC AQHI location joined to the same-named monitor within 10 km (AQHI locations are community points) |

Names are compared with `core.fn_name_key`: letters only, lowercase, doubled letters collapsed.

### core.measurement
Key: `station_id, pollutant, hour_utc, source`. `value` is in canonical units: µg/m³ for `pm25`/`pm10`, ppb for
`o3`/`no2` (OpenAQ's ppm values are ×1000). `coverage_pct` is OpenAQ's percent of the hour covered; `method_code`
is the NAPS instrument method; `qc_flag` is `negative` (value nulled), `above_plausible_max` or `openaq_flagged`.

**`core.v_measurement_best`** picks one value per site, pollutant and hour: NAPS (validated) first, then an OpenAQ
government monitor, then a low-cost sensor.

### Weather
| Table | Key | Notes |
|---|---|---|
| `core.climate_station` | `climate_id` | ECCC climate stations with hourly data in the region |
| `core.weather_hourly` | `climate_id, hour_utc` | observed: temp °C, RH %, wind km/h, wind direction degrees (null when calm), station pressure kPa, precip mm |
| `core.weather_point` | `point_id` | Open-Meteo grid points, ~0.1° (about 10 km) |
| `core.weather_forecast_hourly` | `point_id, hour_utc` | forecast weather; `kind` = hist (archived forecast) or live (latest run). A live value never overwrites a hist one. `blh_m` (boundary layer height) is only in the archive from about late 2024 |
| `core.station_weather_match` | `station_id` (site) | nearest climate station and distance; `is_used = 0` past `MAX_MATCH_KM` (25 km). Also the nearest forecast point |

### Official AQHI
| Table | Key | Notes |
|---|---|---|
| `core.aqhi_obs` | `location_id, observation_utc` | ECCC hourly AQHI; `observation_utc` is the end of the last averaged hour |
| `core.aqhi_forecast` | `location_id, publication_utc, kind, slot` | `kind` hourly (slot = forecast hour) or period (slot = period_1..5, `period_label` Today/Tonight/Tomorrow/...) |

ECCC only serves about three days of both, so these two tables are never truncated, not even by a full reload.

## mart

### mart.daily_station
One row per site per **local** day (America/Toronto, DST handled with `AT TIME ZONE`).

| Columns | Window |
|---|---|
| `pm25_mean`, `pm25_max`, `pm25_hours`, `pm25_completeness`, `no2_mean`, `o3_8h_max`, `aqhi_max` | the whole local day; used as the forecast target and for days before the issue day |
| `pm25_24h_to6_mean/max/hours`, `pm25_6h_to6_mean`, `o3_8h_to6_max`, `no2_24h_to6_mean` | the 24 h (or 6 h) ending 06:00 local on that date: what a forecast issued that morning may use |
| `obs_*` | observed weather at the matched climate station, whole day |
| `fc_*` | forecast weather at the site's grid point for that day (`fc_wind_sin/cos` = mean wind direction components) |

`pm25_completeness` = hours with data / 24. Days under 75% are kept but never used as training targets.
`o3_8h_*` uses 8-hour trailing means that need 6 of 8 hours. `aqhi_max` is the computed AQHI
(`mart.fn_aqhi_hourly`, 3-hour trailing means needing 2 of 3 hours).

### mart.forecast
`site_id, target_date, model_version` → `pm25_pred`, `pm25_p10`, `pm25_p90` (80% range), `aqhi_pred`,
`aqhi_category`, `issued_at`, `run_id`.

## etl

| Table | What it records |
|---|---|
| `etl.pipeline_run` | one row per run: mode, target (local/azure), status, start/end, `duration_s`, rows read/written/inserted/updated, `measurement_rows` at the end, error message |
| `etl.stage_run` | one row per stage and item: plan, extract (rows read/written, seconds), merge (inserted/updated/rejected), link, match, mart, dq |
| `etl.source_state` | watermarks by source and key: NAPS file `Last-Modified`/ETag/SHA-256, max datetimes for AQHI, last hour per climate station, last date per forecast point |
| `etl.sensor_state` | last hour fetched per OpenAQ sensor; only moves forward over contiguous data |
| `etl.dq_result` | each data-quality check per run: severity (error/warn/info), status (pass/warn/fail), observed value, threshold |
