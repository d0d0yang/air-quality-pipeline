"""
predict.py

Loads the latest model and writes tomorrow's PM2.5 forecast (with an 80% band and an AQHI category)
for every site into mart.forecast. Issue time is 06:00 America/Toronto, same as training.
"""

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd

from model import features as F
from pipeline import aqhi, db, storage

LOCAL_TZ = ZoneInfo("America/Toronto")


def load_bundle(store=None):
    store = store or storage.get_storage()
    version = json.loads(store.read_bytes("models/latest.json"))["version"]
    return joblib.load(io.BytesIO(store.read_bytes(f"models/{version}/model.joblib")))


def today_local(now=None):
    return (now or datetime.now(UTC)).astimezone(LOCAL_TZ).date()


def forecast_frame(bundle, mart, issue_date):
    """Feature rows for one issue day -> predictions. Sites without today's data or tomorrow's weather are skipped."""
    rows = F.build(mart, issue_dates=[issue_date])
    rows = rows[rows["fc_temp_mean"].notna() & rows[["pm25_24h_to6_mean", "pm25_prev_day_mean"]].notna().any(axis=1)]
    if rows.empty:
        return rows
    X = rows[bundle["features"]]
    pred = np.clip(bundle["model"].predict(X), 0, None)
    lo = np.clip(bundle["q10"].predict(X), 0, None)
    hi = np.clip(bundle["q90"].predict(X), 0, None)
    # separately fitted quantiles can cross the point forecast; keep the band around it
    lo, hi = np.minimum(lo, pred), np.maximum(hi, pred)
    values = [aqhi.aqhi(n, o, p) for n, o, p in zip(rows["no2_24h_to6_mean"], rows["o3_8h_to6_max"], pred,
                                                     strict=True)]
    return rows.assign(pm25_pred=pred, pm25_p10=lo, pm25_p90=hi, aqhi_pred=values,
                       aqhi_category=[aqhi.category(v) for v in values])


def write(conn, frame, version, run_id=None):
    issued = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    # sites without no2/o3 (low-cost sensors) have no aqhi; pandas turns that None into NaN, sql wants NULL
    records = [(int(r.site_id), r.target_date.date(), issued, float(r.pm25_pred), float(r.pm25_p10),
                float(r.pm25_p90), None if pd.isna(r.aqhi_pred) else float(r.aqhi_pred),
                None if pd.isna(r.aqhi_category) else r.aqhi_category, version, run_id) for r in frame.itertuples()]
    cur = conn.cursor()
    for site_id, target_date, *_ in records:
        cur.execute("DELETE FROM mart.forecast WHERE site_id = ? AND target_date = ? AND model_version = ?",
                    site_id, target_date, version)
    cur.fast_executemany = True
    cur.executemany(
        "INSERT INTO mart.forecast (site_id, target_date, issued_at, pm25_pred, pm25_p10, pm25_p90, aqhi_pred, "
        "aqhi_category, model_version, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
    return len(records)


def run(run_id=None, issue_date=None, conn=None):
    bundle = load_bundle()
    own = conn is None
    conn = conn or db.connect()
    try:
        issue_date = issue_date or today_local()
        frame = forecast_frame(bundle, F.load_mart(conn), pd.Timestamp(issue_date))
        written = write(conn, frame, bundle["version"], run_id) if len(frame) else 0
    finally:
        if own:
            conn.close()
    target = issue_date + timedelta(days=1)
    print(f"model {bundle['version']}: {written} site forecasts for {target}")
    return {"model_version": bundle["version"], "target_date": target.isoformat(), "forecasts": written}


def main(argv=None):
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
