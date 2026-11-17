# Model card: next-day PM2.5 forecast

**Experimental, not health advice.** For decisions about outdoor activity use the official
[Air Quality Health Index](https://www.canada.ca/en/environment-climate-change/services/air-quality-health-index.html)
from Environment and Climate Change Canada.

All numbers below come from `model/artifacts/metrics.json` (model version `20261117-ridge`, generated 2026-11-17).

## What it predicts

Tomorrow's **daily mean PM2.5** (µg/m³, local day in America/Toronto) for each monitoring site in Southern Ontario
(bbox −81.0, 42.8, −78.5, 44.6), issued at 06:00 local. It also gives an 80% range (10th to 90th percentile) and an
AQHI category for tomorrow. The category comes from the predicted PM2.5 plus today's NO2 and O3 carried forward.

## Data

- **Targets and history:** hourly PM2.5, O3 and NO2 from ECCC's NAPS network (validated, 2022-11 to 2024-12) and
  from OpenAQ (government monitors and low-cost sensors, 2025-01 onwards, since NAPS is published with a lag).
  Duplicate sites across sources are folded together, preferring NAPS, then government monitors, then low-cost
  sensors.
- **Weather:** Open-Meteo's archived forecasts (Historical Forecast API) for training, and its live Forecast API
  for tomorrow, at a ~10 km grid point near each site.
- **Training rows:** a site-day is used only when the target day has at least 75% of its hours. Low-cost sensors
  are left out of training and scored separately.

| | Rows (site-days) | Target dates |
|---|---|---|
| Training pool | 21,154 | 2022-11-17 to 2025-11-13 |
| Locked holdout | 6,718 | 2025-11-21 to 2026-11-16 |
| Sites (monitors) | 25 | |

## Features (all known by 06:00 on the issue day)

- PM2.5 over the 24 h and 6 h before 06:00 (mean, max), yesterday's mean, and the 3-day trend
- O3 8-hour max and NO2 mean over the 24 h before 06:00
- regional mean PM2.5 over the same 24 h (all monitors)
- tomorrow's forecast weather: mean and max temperature, mean wind speed, wind direction (sine and cosine),
  humidity, total precipitation, cloud cover
- day of week and month (as sine and cosine), site latitude, longitude, elevation and urbanization class

Leakage guards (`tests/test_features.py`): no feature reads the issue day after 06:00 or anything from the target day
except its weather forecast, and every training target date is before the first validation date
(`tests/test_train.py`). Boundary layer height is stored but not used, because the archive only has it from late
2024.

## Validation

1. **Model choice by backtest only.** A rolling-origin backtest by quarter (8 quarters, each trained on everything
   before it). Mean MAE: Ridge **3.36**, HistGradientBoosting 3.49, RandomForest 3.85 µg/m³. Ridge was chosen.
   The holdout wasn't looked at until this choice was final.
2. **Unseen stations.** 5-fold GroupKFold by site on the training pool: MAE **3.41** µg/m³, skill vs persistence
   **22%**, R² 0.31. Per-site skill ranges from 9% (Egbert, a rural site) to 27% (Toronto West, Hamilton
   Downtown), apart from one US site with only 6 days.
3. **Locked holdout, scored once.** The model was fit on the whole training pool and scored on the last 12 months.

## Results on the holdout year

| Forecast | MAE (µg/m³) | RMSE | R² | Skill vs persistence |
|---|---|---|---|---|
| **Model (Ridge)** | **3.80** | 9.70 | 0.27 | **23%** |
| Persistence (tomorrow = last 24 h) | 4.92 | 12.07 | −0.12 | 0% |
| Climatology (site × month mean) | 4.74 | 11.37 | 0.00 | 4% |

- **Worst 10% of days** (actual daily mean ≥ 13.0 µg/m³, 672 site-days): model MAE 14.9, persistence 18.8,
  climatology 17.2. The model is still the best of the three, but errors on these days are four times the
  overall average.
- **80% range:** it contained the actual value on only **59%** of days (mean width 5.5 µg/m³), so it's too narrow.
  The actual value fell below the 10th percentile on 24% of days and above the 90th on 17%; the target was 10%
  each. The range comes from separately fitted quantile models (HistGradientBoosting, 10th and 90th percentile),
  and its upper edge sat below the Ridge forecast on 22% of days. The published band is widened to include the
  point forecast.
- **AQHI category tomorrow:** right on **79%** of site-days, against 75% for persistence (yesterday's category).
  Most days are "low risk", so both numbers are high.
- **ECCC's official AQHI forecast:** only **1 holdout day** (17 sites) overlaps, because the ECCC API keeps about three
  days and this pipeline only started archiving it on 2026-11-17. On that day both ECCC and the model got 94% of
  categories right. That's too little to compare. The comparison fills in as the archive grows.
- **Low-cost sensors** (57 sites, never trained on): MAE 6.59 vs persistence 7.19 (8% skill). They are noisier, and
  they get their own badge on the map.

## Known failure modes

- **Wildfire smoke.** The worst day in the holdout was 2026-07-16, a smoke day with a regional mean of 152 µg/m³.
  The forecast missed it by 135 µg/m³ on average. Nothing in the features knows about fires hundreds of
  kilometres away, so the model can only catch up once smoke is already in the last 24 hours.
- **Train/serve gap in the weather.** Training uses Open-Meteo's *archived* forecasts, which are stitched from
  short-lead runs. At prediction time, tomorrow's weather comes from a forecast 18–42 h ahead, which is a bit less
  accurate.
- **Source shift.** Most training targets are validated NAPS data, while the holdout and live forecasts use
  real-time OpenAQ data from the same monitors. Real-time data can be revised later.
- **Too-narrow range**, see above. A fix would be to calibrate the range on backtest residuals (conformal
  intervals). That has to be checked on new data, not on this holdout.
- **Sites with little history** (new low-cost sensors, US border sites) get less reliable forecasts.

## Intended use

A portfolio and learning project that shows an end-to-end data pipeline with an honestly validated model. It
shouldn't be used for health decisions, alerts or anything operational.

## Cadence

Retrained weekly (Mondays) or on demand; predictions daily after the refresh. The published model is refit on all
rows (training pool + holdout) after the one-time evaluation above.
