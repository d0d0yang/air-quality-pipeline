"""
test_features.py

Leakage guards: features may only use data known by 06:00 on the issue day. Built on a synthetic mart where
everything a feature must not read is a huge sentinel, so any leak shows up as a giant number.
"""

import numpy as np
import pandas as pd
import pytest

from model import features as F

SENTINEL = 99999.0


def mart(days=8, sites=(1, 2)):
    rows = []
    for site in sites:
        for i, day in enumerate(pd.date_range("2025-03-01", periods=days)):
            rows.append({
                "site_id": site, "local_date": day,
                "pm25_mean": 10.0 + i, "pm25_max": 20.0 + i, "pm25_completeness": 100.0, "aqhi_max": 3.0,
                "pm25_24h_to6_mean": 5.0 + i, "pm25_24h_to6_max": 9.0, "pm25_6h_to6_mean": 4.0,
                "o3_8h_to6_max": 30.0, "no2_24h_to6_mean": 12.0,
                "fc_temp_mean": 1.0 + i, "fc_temp_max": 5.0, "fc_wind_mean": 10.0, "fc_wind_sin": 0.0,
                "fc_wind_cos": 1.0, "fc_rh_mean": 70.0, "fc_precip_sum": 0.0, "fc_cloud_mean": 50.0, "fc_kind": "hist",
                "obs_temp_mean": 2.0,
                "lat": 43.7, "lon": -79.4, "elevation_m": 100.0, "urbanization": "LU", "is_low_cost_sensor": 0,
                "site_name": f"site {site}",
            })
    return pd.DataFrame(rows)


def built(m, issue="2025-03-05"):
    out = F.build(m)
    return out[(out["site_id"] == 1) & (out["issue_date"] == pd.Timestamp(issue))].iloc[0]


def poison(m, site, day, columns):
    m = m.copy()
    mask = (m["site_id"] == site) & (m["local_date"] == pd.Timestamp(day))
    for c in columns:
        m.loc[mask, c] = SENTINEL
    return m


def test_target_is_the_next_local_day_mean():
    row = built(mart())
    assert row["target_date"] == pd.Timestamp("2025-03-06")
    assert row[F.TARGET] == 15.0  # pm25_mean on day index 5


def test_issue_day_full_day_values_never_used():
    # the issue day's whole-day stats include hours after 06:00, so they must not reach any feature
    m = poison(mart(), 1, "2025-03-05", ["pm25_mean", "pm25_max", "aqhi_max", "obs_temp_mean"])
    row = built(m)
    assert (row[F.FEATURES].astype(float).abs() < SENTINEL / 2).all()


def test_target_day_observations_never_used_as_features():
    # tomorrow's measurements only feed the target column
    m = poison(mart(), 1, "2025-03-06", ["pm25_24h_to6_mean", "pm25_24h_to6_max", "pm25_6h_to6_mean",
                                          "o3_8h_to6_max", "no2_24h_to6_mean", "obs_temp_mean"])
    row = built(m)
    assert (row[F.FEATURES].astype(float).abs() < SENTINEL / 2).all()


def test_features_read_the_right_days():
    row = built(mart())
    assert row["pm25_24h_to6_mean"] == 9.0      # issue day (index 4): 5 + 4
    assert row["pm25_prev_day_mean"] == 13.0    # day before the issue day: 10 + 3
    assert row["pm25_3day_trend"] == 13.0 - 11.0
    assert row["fc_temp_mean"] == 6.0           # forecast for the target day (index 5)


def test_low_cost_sensors_stay_out_of_regional_mean():
    m = mart(sites=(1, 2, 3))
    m.loc[m["site_id"] == 3, "is_low_cost_sensor"] = 1
    m.loc[m["site_id"] == 3, "pm25_24h_to6_mean"] = 500.0
    row = built(m)
    assert row["regional_pm25_24h_mean"] == 9.0


def test_incomplete_target_day_is_not_a_target():
    m = mart()
    m.loc[(m["site_id"] == 1) & (m["local_date"] == pd.Timestamp("2025-03-06")), "pm25_completeness"] = 60.0
    assert np.isnan(built(m)[F.TARGET])


def test_training_rows_drop_low_cost_by_default():
    m = mart()
    m.loc[m["site_id"] == 2, "is_low_cost_sensor"] = 1
    rows = F.training_rows(F.build(m))
    assert set(rows["site_id"]) == {1}
    assert len(F.training_rows(F.build(m), include_low_cost=True)["site_id"].unique()) == 2


@pytest.mark.parametrize("column", F.FEATURES)
def test_every_feature_is_built(column):
    assert column in F.build(mart()).columns
