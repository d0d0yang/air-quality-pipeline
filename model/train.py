"""
train.py

Picks a model with a quarterly rolling-origin backtest, checks it on stations it never saw (GroupKFold by site),
then scores it once on the locked last 12 months next to persistence, climatology and ECCC's own forecast.
Writes model/artifacts/metrics.json and the fitted models for predict.py.
"""

import argparse
import io
import json
import sys
from datetime import UTC, datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from model import features as F
from pipeline import aqhi, config, db, storage

ARTIFACT_DIR = config.ROOT / "model" / "artifacts"
HOLDOUT_DAYS = 365
MIN_TRAIN_DAYS = 365  # a backtest fold needs at least a year of history before it
SEED = 0


def candidates():
    return {
        "ridge": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=1.0)),
        "random_forest": make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestRegressor(n_estimators=300, min_samples_leaf=5, n_jobs=-1, random_state=SEED)),
        "hist_gradient_boosting": HistGradientBoostingRegressor(
            max_iter=400, learning_rate=0.05, min_samples_leaf=40, l2_regularization=1.0, random_state=SEED),
    }


def quantile_model(q):
    return HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=400, learning_rate=0.05,
                                         min_samples_leaf=40, random_state=SEED)


# --- metrics ------------------------------------------------------------------------------------


def mae(y, p):
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(p))))


def rmse(y, p):
    return float(np.sqrt(np.mean((np.asarray(y) - np.asarray(p)) ** 2)))


def r2(y, p):
    y, p = np.asarray(y), np.asarray(p)
    ss_res, ss_tot = np.sum((y - p) ** 2), np.sum((y - y.mean()) ** 2)
    return float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")


def skill(model_mae, persistence_mae):
    """1 - MAE_model / MAE_persistence: 0 means no better than 'tomorrow = today', 1 is perfect."""
    return float(1 - model_mae / persistence_mae) if persistence_mae > 0 else float("nan")


def coverage(y, lo, hi):
    y = np.asarray(y)
    return float(np.mean((y >= np.asarray(lo)) & (y <= np.asarray(hi))))


def scores(y, p, persistence=None):
    out = {"n": int(len(y)), "mae": mae(y, p), "rmse": rmse(y, p), "r2": r2(y, p)}
    if persistence is not None:
        out["skill_vs_persistence"] = skill(out["mae"], mae(y, persistence))
    return out


def categories(values):
    return [aqhi.category(v) for v in values]


def category_accuracy(observed, predicted):
    pairs = [(o, p) for o, p in zip(observed, predicted, strict=True) if o is not None and p is not None]
    return {"n": len(pairs), "accuracy": float(np.mean([o == p for o, p in pairs])) if pairs else None}


# --- splits -------------------------------------------------------------------------------------


def holdout_split(rows, days=HOLDOUT_DAYS):
    """The last `days` of target dates are locked away; everything before is the training pool."""
    start = rows["target_date"].max() - pd.Timedelta(days=days - 1)
    pool, holdout = rows[rows["target_date"] < start], rows[rows["target_date"] >= start]
    assert pool["target_date"].max() < holdout["target_date"].min()
    return pool.reset_index(drop=True), holdout.reset_index(drop=True), start


def quarterly_folds(pool, min_train_days=MIN_TRAIN_DAYS):
    """Rolling origin: train on everything before a quarter, validate on that quarter."""
    first = pool["target_date"].min()
    starts = pd.date_range(first + pd.Timedelta(days=min_train_days), pool["target_date"].max(), freq="QS")
    folds = []
    for q in starts:
        end = q + pd.DateOffset(months=3)
        train = pool[pool["target_date"] < q]
        valid = pool[(pool["target_date"] >= q) & (pool["target_date"] < end)]
        if len(valid) and len(train):
            # leakage guard: every training target is before the first validation day
            assert train["target_date"].max() < valid["target_date"].min()
            folds.append((q, train, valid))
    return folds


def climatology(train):
    """Mean target per site and month, falling back to the all-site monthly mean."""
    t = train.assign(month=train["target_date"].dt.month)
    by_site = t.groupby(["site_id", "month"])[F.TARGET].mean()
    by_month = t.groupby("month")[F.TARGET].mean()

    def predict(rows):
        keys = list(zip(rows["site_id"], rows["target_date"].dt.month, strict=True))
        site_vals = by_site.reindex(keys).to_numpy()
        month_vals = by_month.reindex(rows["target_date"].dt.month).to_numpy()
        return np.where(np.isnan(site_vals), month_vals, site_vals)

    return predict


def persistence(rows):
    # "tomorrow = today": the last 24 h known at issue time, falling back to yesterday's mean
    return rows["pm25_24h_to6_mean"].fillna(rows["pm25_prev_day_mean"]).to_numpy()


def predicted_aqhi(rows, pm25_pred):
    """aqhi from predicted pm2.5 with today's no2 and o3 carried forward (persistence)."""
    return [aqhi.aqhi(n, o, p) for n, o, p in zip(rows["no2_24h_to6_mean"], rows["o3_8h_to6_max"], pm25_pred,
                                                    strict=True)]


# --- eccc's official forecast as a baseline -------------------------------------------------------


def eccc_tomorrow(conn):
    """ECCC's 'Tomorrow' AQHI period forecast per linked site, issued on the morning before the target day."""
    sql = """
        SELECT x.site_id,
               CAST(CAST(f.publication_utc AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(0))
                    AS DATE) AS issue_date,
               f.aqhi, f.publication_utc
        FROM core.aqhi_forecast AS f
        JOIN core.station AS a ON a.station_key = CONCAT('aqhi:', f.location_id)
        JOIN core.station_xref AS x ON x.station_id = a.station_id AND x.link_method = 'aqhi_name'
        WHERE f.kind = 'period' AND f.period_label = 'Tomorrow'
    """
    cur = conn.cursor().execute(sql)
    df = pd.DataFrame.from_records(cur.fetchall(), columns=[c[0] for c in cur.description])
    if df.empty:
        return df
    df["issue_date"] = pd.to_datetime(df["issue_date"])
    df["aqhi"] = pd.to_numeric(df["aqhi"])
    # the morning issue (06:00 local) is the one comparable to ours; keep the earliest of each day
    return df.sort_values("publication_utc").groupby(["site_id", "issue_date"], as_index=False).first()


# --- the whole thing ------------------------------------------------------------------------------


def run(write=True, conn=None, score_holdout=True):
    own = conn is None
    conn = conn or db.connect()
    try:
        mart = F.load_mart(conn)
        built = F.build(mart)
        eccc = eccc_tomorrow(conn)
    finally:
        if own:
            conn.close()

    rows = F.training_rows(built)
    low_cost_rows = F.training_rows(built, include_low_cost=True)
    low_cost_rows = low_cost_rows[low_cost_rows["is_low_cost_sensor"] == 1].reset_index(drop=True)
    pool, holdout, holdout_start = holdout_split(rows)
    X = F.FEATURES

    # 1. model selection on the backtest only (the holdout stays untouched)
    folds = quarterly_folds(pool)
    selection = {}
    for name, est in candidates().items():
        fold_scores = []
        for q, train, valid in folds:
            est.fit(train[X], train[F.TARGET])
            pred = est.predict(valid[X])
            fold_scores.append({"quarter": q.strftime("%Y-%m-%d"),
                                **scores(valid[F.TARGET], pred, persistence(valid))})
        selection[name] = {"backtest_mae_mean": float(np.mean([f["mae"] for f in fold_scores])),
                           "folds": fold_scores}
        print(f"backtest {name:24} mae {selection[name]['backtest_mae_mean']:.3f} over {len(fold_scores)} quarters")
    chosen = min(selection, key=lambda n: selection[n]["backtest_mae_mean"])
    print(f"chosen: {chosen}")

    # 2. spatial validation: leave whole sites out
    groups = pool["site_id"]
    n_splits = min(5, groups.nunique())
    spatial_pred = np.full(len(pool), np.nan)
    for train_idx, valid_idx in GroupKFold(n_splits=n_splits).split(pool, groups=groups):
        est = candidates()[chosen]
        est.fit(pool.iloc[train_idx][X], pool.iloc[train_idx][F.TARGET])
        spatial_pred[valid_idx] = est.predict(pool.iloc[valid_idx][X])
    per_site = []
    for site_id, g in pool.assign(pred=spatial_pred, pers=persistence(pool)).groupby("site_id"):
        site_mae = mae(g[F.TARGET], g["pred"])
        per_site.append({"site_id": int(site_id), "name": g["site_name"].iloc[0] if "site_name" in g else None,
                         "lat": float(g["lat"].iloc[0]), "lon": float(g["lon"].iloc[0]), "n": int(len(g)),
                         "mae": site_mae, "skill_vs_persistence": skill(site_mae, mae(g[F.TARGET], g["pers"]))})
    spatial = {"folds": n_splits, **scores(pool[F.TARGET], spatial_pred, persistence(pool)), "per_site": per_site}

    if not score_holdout:
        # development mode: model choice and spatial check only, the holdout stays unseen
        return {"selection": {k: v["backtest_mae_mean"] for k, v in selection.items()}, "chosen_model": chosen,
                "spatial": {k: v for k, v in spatial.items() if k != "per_site"},
                "data": {"pool_rows": int(len(pool)), "holdout_rows_locked": int(len(holdout)),
                         "holdout_start": holdout_start.strftime("%Y-%m-%d"), "folds": len(folds)}}

    # 3. the one-time holdout: fit on the whole pool, score the locked last 12 months
    final = candidates()[chosen].fit(pool[X], pool[F.TARGET])
    q10, q90 = quantile_model(0.1).fit(pool[X], pool[F.TARGET]), quantile_model(0.9).fit(pool[X], pool[F.TARGET])
    y = holdout[F.TARGET].to_numpy()
    pred, lo, hi = final.predict(holdout[X]), q10.predict(holdout[X]), q90.predict(holdout[X])
    pers, clim = persistence(holdout), climatology(pool)(holdout)
    high = y >= np.quantile(y, 0.9)  # top decile of actual days: smoke and other bad days

    observed_cat = categories(holdout["target_aqhi_max"])
    pred_cat = categories(predicted_aqhi(holdout, pred))
    # aqhi persistence: the category of the last full day known at issue time (the day before the issue day;
    # the issue day itself isn't over at 06:00)
    daily_aqhi = (built.drop_duplicates(["site_id", "target_date"])
                  .set_index(["site_id", "target_date"])["target_aqhi_max"])
    known_day = holdout["issue_date"] - pd.Timedelta(days=1)
    pers_cat = categories(daily_aqhi.reindex(list(zip(holdout["site_id"], known_day, strict=True))).to_numpy())

    holdout_metrics = {
        "model": scores(y, pred, pers),
        "persistence": scores(y, pers, pers),
        "climatology": scores(y, clim, pers),
        "high_pollution_days": {
            "threshold_ug_m3": float(np.quantile(y, 0.9)),
            "model": scores(y[high], pred[high], pers[high]),
            "persistence": scores(y[high], pers[high]),
            "climatology": scores(y[high], clim[high]),
        },
        "interval_80": {"coverage": coverage(y, lo, hi), "mean_width": float(np.mean(hi - lo))},
        "aqhi_category": {"model": category_accuracy(observed_cat, pred_cat),
                          "persistence": category_accuracy(observed_cat, pers_cat)},
    }

    # eccc's 'Tomorrow' forecast where it overlaps the holdout (the api only keeps ~3 days, so we archive it)
    if not eccc.empty:
        joined = holdout.assign(model_cat=pred_cat, obs_cat=observed_cat).merge(
            eccc, on=["site_id", "issue_date"], how="inner")
        holdout_metrics["aqhi_category"]["eccc_forecast"] = category_accuracy(
            joined["obs_cat"].tolist(), categories(joined["aqhi"]))
        holdout_metrics["aqhi_category"]["model_on_eccc_days"] = category_accuracy(
            joined["obs_cat"].tolist(), joined["model_cat"].tolist())
        holdout_metrics["aqhi_category"]["eccc_overlap_days"] = int(joined["target_date"].nunique())
    else:
        holdout_metrics["aqhi_category"]["eccc_forecast"] = {"n": 0, "accuracy": None}
        holdout_metrics["aqhi_category"]["eccc_overlap_days"] = 0

    if len(low_cost_rows):
        lc_hold = low_cost_rows[low_cost_rows["target_date"] >= holdout_start]
        if len(lc_hold):
            holdout_metrics["low_cost_sensors"] = {
                "model": scores(lc_hold[F.TARGET], final.predict(lc_hold[X]), persistence(lc_hold)),
                "persistence": scores(lc_hold[F.TARGET], persistence(lc_hold)),
                "sites": int(lc_hold["site_id"].nunique()),
            }

    version = f"{datetime.now(UTC):%Y%m%d}-{chosen}"
    metrics = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_version": version,
        "chosen_model": chosen,
        "features": X,
        "data": {
            "training_rows": int(len(pool)), "holdout_rows": int(len(holdout)), "sites": int(rows["site_id"].nunique()),
            "train_targets": [pool["target_date"].min().strftime("%Y-%m-%d"),
                              pool["target_date"].max().strftime("%Y-%m-%d")],
            "holdout_targets": [holdout["target_date"].min().strftime("%Y-%m-%d"),
                                holdout["target_date"].max().strftime("%Y-%m-%d")],
            "low_cost_rows": int(len(low_cost_rows)),
        },
        "selection": selection,
        "spatial": spatial,
        "holdout": holdout_metrics,
        "notes": [
            "low-cost sensors are excluded from training and scored separately",
            "the deployed model is refit on all rows (training pool + holdout) after this one-time evaluation",
        ],
    }

    if write:
        # after the one honest look at the holdout, refit on everything for the forecasts we publish
        everything = pd.concat([pool, holdout], ignore_index=True)
        bundle = {
            "version": version, "features": X, "chosen": chosen,
            "model": candidates()[chosen].fit(everything[X], everything[F.TARGET]),
            "q10": quantile_model(0.1).fit(everything[X], everything[F.TARGET]),
            "q90": quantile_model(0.9).fit(everything[X], everything[F.TARGET]),
        }
        save(bundle, metrics, holdout.assign(pred=pred, p10=lo, p90=hi, persistence=pers, climatology=clim))
    return metrics


def refit(conn=None):
    """
    The weekly retrain: the already-chosen model refit on every row up to now. Model choice and the holdout
    evaluation (run()) are a deliberate offline step, so the daily function stays well inside its time limit.
    """
    store = storage.get_storage()
    chosen = json.loads(store.read_bytes("models/metrics.json"))["chosen_model"]
    own = conn is None
    conn = conn or db.connect()
    try:
        rows = F.training_rows(F.build(F.load_mart(conn)))
    finally:
        if own:
            conn.close()
    X = F.FEATURES
    version = f"{datetime.now(UTC):%Y%m%d}-{chosen}-refit"
    bundle = {"version": version, "features": X, "chosen": chosen,
              "model": candidates()[chosen].fit(rows[X], rows[F.TARGET]),
              "q10": quantile_model(0.1).fit(rows[X], rows[F.TARGET]),
              "q90": quantile_model(0.9).fit(rows[X], rows[F.TARGET])}
    buf = io.BytesIO()
    joblib.dump(bundle, buf)
    store.write_bytes(f"models/{version}/model.joblib", buf.getvalue())
    store.write_bytes("models/latest.json", json.dumps({"version": version}).encode())
    return {"model_version": version, "rows": int(len(rows))}


def save(bundle, metrics, holdout_frame):
    store = storage.get_storage()
    buf = io.BytesIO()
    joblib.dump(bundle, buf)
    store.write_bytes(f"models/{bundle['version']}/model.joblib", buf.getvalue())
    store.write_bytes("models/latest.json", json.dumps({"version": bundle["version"]}).encode())
    cols = ["site_id", "site_name", "issue_date", "target_date", F.TARGET, "pred", "p10", "p90", "persistence",
            "climatology"]
    store.write_bytes(f"models/{bundle['version']}/holdout_predictions.csv",
                      holdout_frame[cols].to_csv(index=False, date_format="%Y-%m-%d").encode())
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    store.write_bytes("models/metrics.json", json.dumps(metrics, indent=2).encode())
    print(f"saved model {bundle['version']} and metrics.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description="train and evaluate the next-day pm2.5 model")
    parser.add_argument("--dry-run", action="store_true", help="evaluate only, don't save anything")
    parser.add_argument("--no-holdout", action="store_true",
                        help="development: backtest and spatial check only, never touches the holdout")
    args = parser.parse_args(argv)
    if args.no_holdout:
        print(json.dumps(run(write=False, score_holdout=False), indent=2))
        return 0
    metrics = run(write=not args.dry_run)
    h = metrics["holdout"]
    print(json.dumps({k: h[k] for k in ("model", "persistence", "climatology")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
