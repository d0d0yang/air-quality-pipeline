"""
features.py

Builds one row per site and issue day from mart.daily_station. A forecast is issued at 06:00 local on the
issue day for the whole next day, so features only use the issue day's "to6" columns (the 24 h before 06:00),
full days before the issue day, and the weather forecast for the target day. No peeking at tomorrow.
"""

from datetime import timedelta

import numpy as np
import pandas as pd

from pipeline import db

MIN_COMPLETENESS = 75.0

# what each feature is allowed to read. tests/test_features.py checks the builder against this.
ISSUE_DAY_COLUMNS = ["pm25_24h_to6_mean", "pm25_24h_to6_max", "pm25_6h_to6_mean", "o3_8h_to6_max",
                     "no2_24h_to6_mean"]
TARGET_DAY_FORECAST_COLUMNS = ["fc_temp_mean", "fc_temp_max", "fc_wind_mean", "fc_wind_sin", "fc_wind_cos",
                               "fc_rh_mean", "fc_precip_sum", "fc_cloud_mean"]
FEATURES = (
    ISSUE_DAY_COLUMNS
    + ["pm25_prev_day_mean", "pm25_3day_trend", "regional_pm25_24h_mean"]
    + TARGET_DAY_FORECAST_COLUMNS
    + ["dow_sin", "dow_cos", "month_sin", "month_cos", "lat", "lon", "elevation_m", "urban"]
)
TARGET = "target_pm25_mean"


def load_mart(conn=None):
    """mart.daily_station joined with site metadata, as a dataframe."""
    sql = """
        SELECT d.*, s.lat, s.lon, s.elevation_m, s.urbanization, s.is_low_cost_sensor, s.name AS site_name,
               s.source AS site_source
        FROM mart.daily_station AS d
        JOIN core.v_site AS s ON s.site_id = d.site_id
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.cursor().execute(sql)
        cols = [c[0] for c in cur.description]
        df = pd.DataFrame.from_records(cur.fetchall(), columns=cols)
    finally:
        if own:
            conn.close()
    return coerce(df)


def coerce(df):
    """Decimals from the driver become floats; dates become pandas timestamps."""
    df = df.copy()
    df["local_date"] = pd.to_datetime(df["local_date"])
    for col in df.columns:
        if col in ("local_date", "urbanization", "site_name", "site_source", "fc_kind"):
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build(mart, issue_dates=None):
    """
    One row per (site_id, issue_date) with the features and, where known, the target.
    issue_dates limits which issue days are built (predict.py asks for just today).
    """
    m = mart.set_index(["site_id", "local_date"]).sort_index()
    rows = mart[["site_id", "local_date"]].rename(columns={"local_date": "issue_date"})
    if issue_dates is not None:
        wanted = pd.to_datetime(pd.Series(list(issue_dates)))
        rows = rows[rows["issue_date"].isin(wanted)]
    rows = rows.drop_duplicates().reset_index(drop=True)

    def take(day_offset, columns):
        keys = pd.MultiIndex.from_arrays([rows["site_id"], rows["issue_date"] + timedelta(days=day_offset)])
        return m.reindex(keys)[columns].reset_index(drop=True)

    site_columns = ["lat", "lon", "elevation_m", "urbanization", "is_low_cost_sensor", "site_name"]
    issue = take(0, ISSUE_DAY_COLUMNS + site_columns)
    prev = take(-1, ["pm25_mean"])
    prev3 = take(-3, ["pm25_mean"])
    target = take(1, TARGET_DAY_FORECAST_COLUMNS + ["pm25_mean", "pm25_completeness", "aqhi_max", "fc_kind"])

    out = pd.concat([rows, issue], axis=1)
    out["target_date"] = out["issue_date"] + timedelta(days=1)
    out["pm25_prev_day_mean"] = prev["pm25_mean"].to_numpy()
    out["pm25_3day_trend"] = (prev["pm25_mean"] - prev3["pm25_mean"]).to_numpy()
    for col in TARGET_DAY_FORECAST_COLUMNS + ["fc_kind"]:
        out[col] = target[col].to_numpy()

    # regional picture at issue time: mean of every monitor's last 24 h (low-cost sensors left out)
    monitors = out[out["is_low_cost_sensor"] == 0]
    regional = monitors.groupby("issue_date")["pm25_24h_to6_mean"].mean().rename("regional_pm25_24h_mean")
    out = out.merge(regional, left_on="issue_date", right_index=True, how="left")

    dow = out["target_date"].dt.day_of_week
    month = out["target_date"].dt.month
    out["dow_sin"], out["dow_cos"] = np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7)
    out["month_sin"], out["month_cos"] = np.sin(2 * np.pi * (month - 1) / 12), np.cos(2 * np.pi * (month - 1) / 12)
    # naps urbanization codes: LU large urban, MU medium, SU small, NU non-urban
    out["urban"] = out["urbanization"].map({"LU": 3, "MU": 2, "SU": 1, "NU": 0})

    # the target: tomorrow's mean, only on days with enough hours to trust it
    complete = target["pm25_completeness"].to_numpy() >= MIN_COMPLETENESS
    out[TARGET] = np.where(complete, target["pm25_mean"].to_numpy(), np.nan)
    out["target_aqhi_max"] = target["aqhi_max"].to_numpy()
    return out.reset_index(drop=True)


def training_rows(features, include_low_cost=False):
    """Rows usable for training: a target, today's pm2.5, and tomorrow's forecast weather."""
    ok = features[TARGET].notna() & features["pm25_24h_to6_mean"].notna() & features["fc_temp_mean"].notna()
    if not include_low_cost:
        ok &= features["is_low_cost_sensor"] == 0
    return features[ok].reset_index(drop=True)
