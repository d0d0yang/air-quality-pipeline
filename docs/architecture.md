# Architecture

How the pipeline is put together, why it's split the way it is, and the JSON the dashboard reads.
Table-level detail is in [data-dictionary.md](data-dictionary.md); the model is in [model-card.md](model-card.md).

## The daily flow

```
 06:00 America/Toronto: ADF trigger tr_refresh
   1  etl.usp_start_run(@mode)                      run_id (full mode empties the fact tables)
   2  /api/plan                                     watermarks -> work items
   3  for each work item                            OpenAQ one at a time (rate limit), the rest 4 at a time
        a  /api/extract                             download + python cleaning -> landing/<source>/<run>/<item>.csv
        b  Copy (local: pyodbc bulk insert)         csv -> stg.<table>, only that item's rows replaced
        c  core.usp_merge_*                         type, dedupe, MERGE on the natural key when row_hash changed
        d  etl.usp_apply_watermarks                 only after the merge worked
   4  core.usp_link_stations, usp_match_stations    same site across sources; nearest weather station
   5  mart.usp_refresh                              daily features per site (only days this run touched)
   6  etl.usp_run_dq_checks                         a failed 'error' check fails the run
   7  /api/model                                    retrain on mondays, predict tomorrow -> mart.forecast
   8  /api/publish                                  mart -> small json files in blob publish/
   9  etl.usp_end_run                               also on every failure path, with the error
 ~13:00 UTC: GitHub Action pulls publish/*.json (read-only SAS), commits frontend/data/, deploys Pages
```

`python -m pipeline.run` runs the same steps on a laptop: SQL Server in Docker, a local `data/` folder instead of
blob storage, and pyodbc bulk inserts instead of the Copy activity. The SQL procs are the same ones ADF calls.

## Who does what

| Layer | Does | Why there |
|---|---|---|
| Python (`pipeline/*_engine.py`, `clean_engine.py`) | HTTP, paging, rate limits, reshaping wide NAPS files, units, sentinels, time zones | APIs and messy files are easier in Python than in SQL |
| SQL (`sql/`) | typing, dedupe, MERGE with row hashes, station linking, nearest-station matching, data-quality checks, marts | set work on millions of rows belongs in the database |
| ADF | scheduling, fan-out over work items, bulk Copy, retries, the run log on failure | what ADF is for, and it shows real ADF usage |
| scikit-learn (`model/`) | features, backtest, holdout, forecasts | |
| Static site (`frontend/`) | three pages reading committed JSON | no API to secure, no database to wake up, works with Azure turned off |

Azure Functions called from ADF must answer within about 230 s, so `/api/plan` sizes every item to finish in
under 200 s. An OpenAQ item is at most 100 requests (about 120 s at 50 requests a minute). The rest are one
file, one station-year, or ten forecast points for one year. The heaviest item measured locally used 104 MB.

## Incremental loading

Each source has its own watermark (`pipeline/sources.py`, `etl.source_state`, `etl.sensor_state`):

| Source | Full | Incremental |
|---|---|---|
| NAPS hourly files | every pollutant × year file in the window | skipped unless the portal's last-modified date changed; then ETag, then SHA-256 |
| OpenAQ sensors | every sensor from the NAPS handover (2025-01-01) in 40-day requests | from each sensor's last hour minus 48 h |
| GeoMet AQHI | everything the API still has (about 3 days) | since the newest timestamp minus 48 h |
| GeoMet climate hourly | one item per station-year | since each station's last hour minus 48 h |
| Open-Meteo archived forecasts | every grid point for the whole window, one year per item | the last two days |

The 48-hour overlap catches late and corrected values. Merges only update rows whose hash changed, so the
overlap never creates duplicates. A sensor watermark only moves forward over contiguous data: if an item fails,
the next run starts where the data really stops instead of skipping a gap.

Full mode truncates the fact tables first and reloads everything. Stations are merged rather than truncated so
site ids stay stable for the forecast history. The ECCC AQHI archive is never truncated, because ECCC only serves
about three days of it.

## Decisions worth knowing

- **NAPS + OpenAQ.** NAPS is validated but published about a year late (the newest hourly year is 2024). OpenAQ
  fills 2025 onwards from the same government monitors (linked to NAPS sites within 250 m) plus low-cost sensors.
- **Hour convention.** Everything is stored as the start of the hour in UTC. NAPS files are "hour ending, local
  standard time", so H01 on day D at a UTC−5 station is D 05:00 UTC. ECCC's AQHI timestamps mark the end of the
  hour; reproducing their values confirmed that (mean difference 0.006).
- **Weather grid.** Forecast weather is fetched for ~10 km grid points rather than every station. 61 points
  instead of 115 keep a full backfill (~6,600 weighted calls) under Open-Meteo's ~10,000/day fair use.
- **AQHI locations aren't monitor sites.** ECCC's AQHI coordinates are community points 1–7 km from the
  monitors, so they're linked by name within 10 km, not by the 250 m rule.
- **Least privilege.** Everything runs as `etl_app`, which can read and write data and run procs but can't change
  the schema. Only `python -m pipeline.db apply` uses the admin login.
- **No live API for the dashboard.** The database auto-pauses on the free tier; waking it for page views would
  burn free vCore seconds. Publishing small JSON once a day costs nothing to serve.

## Dashboard JSON

All files are written by `pipeline/publish.py` to `publish/` (blob) and, locally, to `frontend/data/`.
`publish.validate()` checks the top-level keys below before anything is written; the Pages workflow runs the same
check before committing. Times are UTC ISO 8601.

### summary.json
```
generated_utc, last_run {run_id, mode, ended_utc, duration_s},
region {bbox [lon_min, lat_min, lon_max, lat_max], pollutants},
sites {total, monitors, low_cost},
regional {pm25_latest_mean, aqhi_max, forecast_pm25_mean, forecast_target_date},
attributions [{provider, licence, attribution, stations}]
```

### stations.json (map page)
```
generated_utc, hours_start_utc,
stations [{id, name, city, lat, lon, low_cost, sources [naps|openaq|aqhi], providers,
           aqhi {value, kind official|computed, time_utc, label, category, colour} | null,
           pm25_latest, pm25_hourly [168 values from hours_start_utc, null for gaps],
           forecast {target_date, pm25, p10, p90, aqhi, category, colour} | null,
           weather_station {name, distance_km, used} | null}]
```

### forecast.json (forecast page)
```
generated_utc, model_version, chosen_model, data {training_rows, holdout_rows, sites, train_targets, holdout_targets},
metrics {model, persistence, climatology: {n, mae, rmse, r2, skill_vs_persistence}},
holdout {high_pollution_days, interval_80, aqhi_category, low_cost_sensors},
selection {candidate: backtest mean MAE}, spatial {folds, n, mae, rmse, r2, skill_vs_persistence, per_site [...]},
sites [{id, name, days [[date, actual, predicted], ...]}], day_columns
```

### pipeline.json (pipeline page)
```
generated_utc,
runs [{run_id, mode, target, trigger, status, started_utc, ended_utc, duration_s, rows_read, rows_written,
       rows_inserted, rows_updated, items, error}]  (last 30),
freshness [{source, latest_utc, rows}], dq [{check, severity, status, observed, threshold, detail}],
last_run_sources [{source, items, rows_read, seconds}], row_counts {table: rows}
```

### benchmark.json (pipeline page)
Written by `benchmarks/refresh_benchmark.py`, not by publish: every timed run plus
`targets {local|azure: {scenarios {full|incremental_daily|incremental_noop: {runs [seconds], median_s}}, reduction_pct}}`.
