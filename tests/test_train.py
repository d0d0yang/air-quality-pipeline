"""
test_train.py

Metric definitions, the split rules (holdout and backtest folds never overlap), and the baselines.
"""

import numpy as np
import pandas as pd
import pytest

from model import features as F
from model import train


def test_metrics_by_hand():
    y, p = [1.0, 2.0, 3.0, 4.0], [1.0, 3.0, 3.0, 2.0]
    assert train.mae(y, p) == 0.75
    assert train.rmse(y, p) == pytest.approx(np.sqrt(5 / 4))
    assert train.r2(y, y) == 1.0
    assert train.skill(1.0, 2.0) == 0.5
    assert train.skill(2.0, 2.0) == 0.0
    assert train.coverage([1, 5, 9], [0, 0, 0], [6, 6, 6]) == pytest.approx(2 / 3)


def test_category_accuracy_skips_missing():
    got = train.category_accuracy(["low", "high", None], ["low", "moderate", "low"])
    assert got == {"n": 2, "accuracy": 0.5}


def rows(days=900, sites=3):
    dates = pd.date_range("2022-01-01", periods=days)
    frame = pd.DataFrame([{"site_id": s, "target_date": d, "issue_date": d - pd.Timedelta(days=1),
                           F.TARGET: float(s + d.month), "pm25_24h_to6_mean": float(d.month),
                           "pm25_prev_day_mean": 1.0}
                          for s in range(sites) for d in dates])
    return frame


def test_holdout_is_the_last_365_days_and_never_overlaps():
    pool, holdout, start = train.holdout_split(rows())
    assert (holdout["target_date"].max() - holdout["target_date"].min()).days == 364
    assert pool["target_date"].max() < start <= holdout["target_date"].min()


def test_quarterly_folds_train_strictly_before_validation():
    pool, _, _ = train.holdout_split(rows())
    folds = train.quarterly_folds(pool)
    assert folds
    for q, tr, va in folds:
        assert tr["target_date"].max() < va["target_date"].min()
        assert (tr["target_date"].max() - tr["target_date"].min()).days >= train.MIN_TRAIN_DAYS - 1
        assert va["target_date"].min() >= q


def test_climatology_uses_site_month_then_month():
    pool = rows(days=400)
    predict = train.climatology(pool)
    new = pd.DataFrame({"site_id": [1, 99], "target_date": pd.to_datetime(["2023-03-10", "2023-03-10"])})
    got = predict(new)
    assert got[0] == 1 + 3                  # site 1, march
    assert got[1] == pytest.approx(1 + 3)   # unknown site: mean over sites 0..2 in march


def test_persistence_falls_back_to_yesterday():
    frame = pd.DataFrame({"pm25_24h_to6_mean": [5.0, np.nan], "pm25_prev_day_mean": [7.0, 8.0]})
    assert list(train.persistence(frame)) == [5.0, 8.0]
