# Air Quality Pipeline

A daily pipeline that pulls hourly air pollution and weather data for Southern Ontario into a SQL database, cleans
and links it, and forecasts tomorrow's PM2.5 at every monitoring station. Results show up on a small public map.

Live dashboard (TBD) · [Pipeline runs](frontend/pipeline.html) · [Model card](docs/model-card.md)

ss

## Functionality

Every morning at 6:00 (Toronto time) an Azure Data Factory pipeline asks each source what's new since the last
run, downloads just that, cleans it in Python, loads it into Azure SQL Database and merges it with T-SQL. It then
links stations that are really the same site, matches each one to its nearest weather station, rebuilds the daily
features, runs data-quality checks, forecasts tomorrow, and publishes a few small JSON files. A GitHub Action
picks those up and redeploys the dashboard, which never touches the database.

- **Sources**: Environment and Climate Change Canada's NAPS network (validated hourly history), its AQHI
  observations and forecasts and hourly climate observations (MSC GeoMet), OpenAQ for recent monitor and
  low-cost sensor data, and Open-Meteo for archived and live weather forecasts.
- **Cleaning**: Python reshapes NAPS's wide "one row per day, 24 hour columns" files, drops the `-999`
  sentinels, converts local standard time to UTC, turns OpenAQ's ppm into ppb, and flags impossible values.
- **Station matching**: SQL links the same physical site across sources (within 250 m and the same name), picks
  NAPS over government monitors over low-cost sensors, and finds each site's nearest weather station.
- **Incremental loads**: each source has a watermark (file ETags, last hour per sensor, latest timestamps), so a
  normal day downloads a couple of days of overlap instead of four years. Merges only touch rows that changed.
- **Forecast**: a scikit-learn model predicts tomorrow's daily mean PM2.5 per station from today's readings and
  tomorrow's weather forecast. It was picked on a quarterly backtest and scored once on a held-out year.
- **Dashboard**: plain HTML/CSS/JS with three pages: a map of current AQHI and tomorrow's forecast, how good the
  forecast is, and every pipeline run.

## Results

Refresh time, full reload vs incremental (median of repeated runs, whole pipeline start to end):

| Target | Full reload | Incremental (normal day) | Reduction |
|---|---|---|---|
| Local (Docker SQL Server) | TBD | 3.0 min (178 s) | TBD |
| Azure (Data Factory + Azure SQL) | TBD | TBD | TBD |

Incremental is the median of 3 runs with a day of watermarks rewound. Full reloads are TBD: the benchmark only runs
one a day to stay inside the APIs' fair use (`benchmarks/refresh_benchmark.py`, all runs in `benchmarks/results.json`).

Next-day PM2.5 forecast on the held-out year (2025-11-21 to 2026-11-16, 6,718 station-days, scored once):

| Forecast | MAE (µg/m³) | RMSE | Skill vs persistence |
|---|---|---|---|
| Model (Ridge regression) | **3.80** | 9.70 | 23% |
| Persistence (tomorrow = today) | 4.92 | 12.07 | 0% |
| Climatology (station × month average) | 4.74 | 11.37 | 4% |
| ECCC AQHI forecast | not comparable yet (1 overlapping day) | | |

The model cuts the error by 23% against persistence and 20% against climatology, and by 22% on stations it
never trained on.
It's still far off on smoke days, and its 80% range is too narrow; the [model card](docs/model-card.md) has the
details.

## Personal Setup

You need Python 3.13, Docker Desktop, Microsoft ODBC Driver 18 for SQL Server, and a free OpenAQ API key.

```
git clone https://github.com/d0d0yang/air-quality-pipeline.git
cd air-quality-pipeline
```

```
python -m venv .venv
.venv\Scripts\activate            (mac/linux: source .venv/bin/activate)
pip install -r requirements.txt -r requirements-dev.txt
```

```
copy .env.example .env            (mac/linux: cp .env.example .env)
then set LOCAL_SA_PASSWORD, LOCAL_ETL_PASSWORD and OPENAQ_API_KEY in .env
```

```
docker compose up -d
python -m pipeline.db apply
```

```
python -m pipeline.run --plan-only
python -m pipeline.run --mode full
```

```
python -m http.server 8080 -d frontend
```

Then open http://localhost:8080.

Note: the first full load took about 50 minutes on my laptop, most of it waiting on OpenAQ's rate limit (about
1,650 requests at 50 a minute). After that, `python -m pipeline.run` (incremental) takes about 3 minutes.

## Deployment

The same code runs on Azure: Data Factory schedules and fans out the work, Azure Functions (Flex Consumption) run
the Python, Blob Storage holds the landing files, and Azure SQL Database runs on its free offer set to pause
instead of bill. The Bicep templates and a one-command `infra/deploy.py` are being built next (TBD).

## Project Structure

```
air-quality-pipeline/
  pipeline/
    config.py                   env vars and defaults
    sources.py                  registry of every source and how to load it
    plan.py                     turns watermarks into small work items
    naps_engine.py              eccc naps hourly files + station list
    openaq_engine.py            openaq v3 client, throttled to the rate limit
    geomet_engine.py            eccc geomet: aqhi + hourly climate observations
    openmeteo_engine.py         weather forecasts, archived and live
    clean_engine.py             python cleaning: reshape, units, sentinels, utc
    extract.py                  one work item -> one clean landing csv
    storage.py                  local folder or blob storage, same interface
    db.py                       sql connection, bulk load, apply sql files
    watermark.py                works out what's new since last run
    run.py                      local runner, mirrors the adf pipeline step for step
    publish.py                  builds the dashboard json from mart tables
    aqhi.py                     aqhi formula, checked against eccc values
  model/
    features.py                 daily features per station, no peeking at tomorrow
    train.py                    backtests, spatial cv, holdout, intervals
    predict.py                  writes mart.forecast
  sql/
    20_procs_merge.sql          sql cleaning + merge per source
    22_stations.sql             station linking + nearest weather station
    25_dq_checks.sql            data quality checks
    30_marts.sql                daily features per site
    40_run_procs.sql            run log, watermarks, benchmark helper
  benchmarks/
    refresh_benchmark.py        times full vs incremental refreshes
  frontend/
    index.html                  map: today + tomorrow
    forecast.html               how good the forecast is
    pipeline.html               run history + full vs incremental
    data/                       json snapshot, refreshed daily by github actions
  docs/
    architecture.md             how it fits together, json schemas
    data-dictionary.md          every table and column
    model-card.md               the model, validated honestly
```

## Tools

Python with pandas, requests, pyodbc and scikit-learn for the extraction, cleaning and model; T-SQL on SQL Server
2022 locally and Azure SQL Database in the cloud for merges, matching, data-quality checks and marts; Azure Data
Factory, Azure Functions and Blob Storage for the cloud pipeline, deployed with Bicep; pytest, responses and ruff
for testing; GitHub Actions for CI and the daily dashboard refresh. Plain HTML/CSS/JS on the frontend, no
framework, with Leaflet and Chart.js.

## Data Sources

- **Environment and Climate Change Canada**: NAPS, AQHI observations and forecasts, and climate observations,
  under the Open Government Licence – Canada.
- **OpenAQ** (openaq.org): data from each upstream provider under its own licence, as reported by the API:
  AirNow (US public domain), AirGradient (CC BY 4.0), Clarity (CC0 1.0), SPARTAN Network (none stated).
- **Open-Meteo** (open-meteo.com): weather forecasts under CC BY 4.0.
- **OpenStreetMap** contributors: map tiles, under the ODbL.

## Known Limitations

- Wildfire smoke doesn't check the forecast before showing up. The worst day in the holdout year (152 µg/m³)
  was missed by more than 100 µg/m³, because nothing in the features knows about fires far away.
- The 80% forecast range only caught the real value on 59% of days, so it's too narrow.
- NAPS publishes validated data about a year late, so recent data comes from OpenAQ's real-time feed, which can
  be revised.
- ECCC's API only keeps about three days of its own AQHI forecasts, so the comparison against them only grows
  from the day this pipeline started archiving them.

Note this is a personal project built for learning purposes rather than a production tool. Forecasts are not health
advice; check the official AQHI at https://www.canada.ca/en/environment-climate-change/services/air-quality-health-index.html
before making decisions about outdoor activity.
