"""
publish.py

Builds the small JSON files the dashboard reads (the dashboard never touches the database). Writes them to
publish/ in storage and, when running locally, straight into frontend/data/ too. Schemas: docs/architecture.md.
"""

import csv
import io
import json
import math
import sys
from datetime import UTC, datetime, timedelta

from pipeline import aqhi, config, db, storage

FRONTEND_DATA = config.ROOT / "frontend" / "data"
MAP_HOURS = 7 * 24
OFFICIAL_AQHI_MAX_AGE_H = 3

# the keys each file must have; validate() checks them and the pages workflow runs it before committing
SCHEMA = {
    "summary.json": ["generated_utc", "last_run", "region", "sites", "regional", "attributions"],
    "stations.json": ["generated_utc", "hours_start_utc", "stations"],
    "forecast.json": ["generated_utc", "model_version", "metrics", "holdout", "spatial", "sites"],
    "pipeline.json": ["generated_utc", "runs", "freshness", "dq", "row_counts"],
}


def rows(conn, sql, *params):
    cur = conn.cursor().execute(sql, *params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def num(value, digits=2):
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else round(value, digits)


def iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value.isoformat()


# --- summary + map ------------------------------------------------------------------------------


def build_stations(conn, now):
    start = (now - timedelta(hours=MAP_HOURS)).replace(minute=0, second=0, microsecond=0)
    sites = rows(conn, """
        SELECT s.site_id, s.name, s.city, s.lat, s.lon, s.source, s.is_low_cost_sensor, s.provider,
               m.climate_id, c.name AS climate_name, m.climate_distance_km, m.is_used, m.point_id
        FROM core.v_site AS s
        LEFT JOIN core.station_weather_match AS m ON m.station_id = s.site_id
        LEFT JOIN core.climate_station AS c ON c.climate_id = m.climate_id
        WHERE s.source <> 'aqhi'
           OR EXISTS (SELECT 1 FROM core.aqhi_obs AS o
                      JOIN core.station AS a ON a.station_key = CONCAT('aqhi:', o.location_id)
                      WHERE a.station_id = s.site_id)
    """)
    members = rows(conn, """
        SELECT x.site_id, s.source, s.name, s.provider, s.licence
        FROM core.station_xref AS x JOIN core.station AS s ON s.station_id = x.station_id
    """)
    hourly = rows(conn, """
        SELECT site_id, hour_utc, value FROM core.v_measurement_best
        WHERE pollutant = 'pm25' AND hour_utc >= ? ORDER BY site_id, hour_utc
    """, start.replace(tzinfo=None))
    official = rows(conn, """
        SELECT x.site_id, o.observation_utc, o.aqhi
        FROM core.aqhi_obs AS o
        JOIN core.station AS a ON a.station_key = CONCAT('aqhi:', o.location_id)
        JOIN core.station_xref AS x ON x.station_id = a.station_id
        WHERE o.observation_utc >= DATEADD(HOUR, -?, SYSUTCDATETIME())
    """, OFFICIAL_AQHI_MAX_AGE_H)
    computed = rows(conn, "SELECT site_id, hour_utc, aqhi FROM mart.fn_aqhi_hourly("
                          "DATEADD(HOUR, -6, SYSUTCDATETIME()), DATEADD(HOUR, 1, SYSUTCDATETIME()))")
    forecasts = rows(conn, """
        SELECT f.site_id, f.target_date, f.pm25_pred, f.pm25_p10, f.pm25_p90, f.aqhi_pred, f.aqhi_category,
               f.model_version, f.issued_at
        FROM mart.forecast AS f
        WHERE f.target_date = (SELECT MAX(target_date) FROM mart.forecast)
          AND f.issued_at = (SELECT MAX(issued_at) FROM mart.forecast g WHERE g.site_id = f.site_id
                             AND g.target_date = f.target_date)
    """)

    series = {}
    for r in hourly:
        idx = int((r["hour_utc"].replace(tzinfo=UTC) - start).total_seconds() // 3600)
        if 0 <= idx < MAP_HOURS:
            series.setdefault(r["site_id"], [None] * MAP_HOURS)[idx] = num(r["value"], 1)
    latest_official = {}
    for r in sorted(official, key=lambda r: r["observation_utc"]):
        latest_official[r["site_id"]] = r
    latest_computed = {}
    for r in sorted(computed, key=lambda r: r["hour_utc"]):
        latest_computed[r["site_id"]] = r
    by_site_members = {}
    for m in members:
        by_site_members.setdefault(m["site_id"], []).append(m)
    fc = {f["site_id"]: f for f in forecasts}

    out = []
    for s in sites:
        sid = s["site_id"]
        values = series.get(sid)
        if values is None and sid not in latest_official and sid not in fc:
            continue  # nothing recent to show (old registrations, closed stations)
        if sid in latest_official:
            a = latest_official[sid]
            current = {"value": num(a["aqhi"], 1), "kind": "official", "time_utc": iso(a["observation_utc"])}
        elif sid in latest_computed:
            a = latest_computed[sid]
            # our hours are hour-beginning; stamp it the way eccc does (end of the hour)
            current = {"value": num(a["aqhi"], 1), "kind": "computed",
                       "time_utc": iso(a["hour_utc"] + timedelta(hours=1))}
        else:
            current = None
        if current:
            current.update(label=aqhi.label(current["value"]), category=aqhi.category(current["value"]),
                           colour=aqhi.colour(current["value"]))
        f = fc.get(sid)
        last_pm = next((v for v in reversed(values) if v is not None), None) if values else None
        out.append({
            "id": sid, "name": s["name"], "city": s["city"], "lat": num(s["lat"], 5), "lon": num(s["lon"], 5),
            "low_cost": bool(s["is_low_cost_sensor"]),
            "sources": sorted({m["source"] for m in by_site_members.get(sid, [])}),
            "providers": sorted({m["provider"] for m in by_site_members.get(sid, []) if m["provider"]}),
            "aqhi": current,
            "pm25_latest": last_pm,
            "pm25_hourly": values,
            "forecast": None if not f else {
                "target_date": iso(f["target_date"]), "pm25": num(f["pm25_pred"], 1), "p10": num(f["pm25_p10"], 1),
                "p90": num(f["pm25_p90"], 1), "aqhi": num(f["aqhi_pred"], 1), "category": f["aqhi_category"],
                "colour": aqhi.colour(f["aqhi_pred"]) if f["aqhi_pred"] is not None else None,
            },
            "weather_station": None if not s["climate_id"] else {
                "name": s["climate_name"], "distance_km": num(s["climate_distance_km"], 1), "used": bool(s["is_used"]),
            },
        })
    return {"generated_utc": iso(now), "hours_start_utc": iso(start), "stations": out}


def build_summary(conn, now, stations):
    last_run = rows(conn, "SELECT TOP (1) run_id, mode, status, started_at, ended_at, duration_s FROM etl.pipeline_run "
                          "WHERE status = 'succeeded' ORDER BY run_id DESC")
    # one line per provider and licence (each low-cost sensor has its own owner, which would repeat providers)
    attributions = rows(conn, """
        SELECT provider, licence, MIN(attribution) AS attribution, COUNT(*) AS stations FROM core.station
        WHERE provider IS NOT NULL GROUP BY provider, licence ORDER BY COUNT(*) DESC
    """)
    with_aqhi = [s for s in stations["stations"] if s["aqhi"]]
    latest_pm = [s["pm25_latest"] for s in stations["stations"] if s["pm25_latest"] is not None and not s["low_cost"]]
    forecasts = [s["forecast"]["pm25"] for s in stations["stations"] if s["forecast"] and not s["low_cost"]]
    r = last_run[0] if last_run else None
    return {
        "generated_utc": iso(now),
        "last_run": None if not r else {"run_id": r["run_id"], "mode": r["mode"], "ended_utc": iso(r["ended_at"]),
                                        "duration_s": num(r["duration_s"], 1)},
        "region": {"bbox": list(config.REGION_BBOX), "pollutants": config.POLLUTANTS},
        "sites": {"total": len(stations["stations"]),
                  "monitors": sum(1 for s in stations["stations"] if not s["low_cost"]),
                  "low_cost": sum(1 for s in stations["stations"] if s["low_cost"])},
        "regional": {
            "pm25_latest_mean": num(sum(latest_pm) / len(latest_pm), 1) if latest_pm else None,
            "aqhi_max": max((s["aqhi"]["value"] for s in with_aqhi if s["aqhi"]["value"] is not None), default=None),
            "forecast_pm25_mean": num(sum(forecasts) / len(forecasts), 1) if forecasts else None,
            "forecast_target_date": next((s["forecast"]["target_date"] for s in stations["stations"] if s["forecast"]),
                                         None),
        },
        "attributions": [{"provider": a["provider"], "licence": a["licence"], "attribution": a["attribution"],
                          "stations": a["stations"]} for a in attributions],
    }


# --- forecast page ------------------------------------------------------------------------------


def build_forecast(store):
    try:
        metrics = json.loads(store.read_bytes("models/metrics.json"))
    except (FileNotFoundError, OSError):
        return None
    version = metrics["model_version"]
    preds = list(csv.DictReader(io.StringIO(store.read_bytes(f"models/{version}/holdout_predictions.csv").decode())))
    sites = {}
    for r in preds:
        s = sites.setdefault(r["site_id"], {"id": int(r["site_id"]), "name": r["site_name"], "days": []})
        s["days"].append([r["target_date"], num(r["target_pm25_mean"], 1), num(r["pred"], 1)])
    h = metrics["holdout"]
    return {
        "generated_utc": metrics["generated_utc"],
        "model_version": version,
        "chosen_model": metrics["chosen_model"],
        "data": metrics["data"],
        "metrics": {k: h[k] for k in ("model", "persistence", "climatology")},
        "holdout": {k: h[k] for k in ("high_pollution_days", "interval_80", "aqhi_category") if k in h}
        | ({"low_cost_sensors": h["low_cost_sensors"]} if "low_cost_sensors" in h else {}),
        "selection": {k: v["backtest_mae_mean"] for k, v in metrics["selection"].items()},
        "spatial": metrics["spatial"],
        "sites": sorted(sites.values(), key=lambda s: s["name"] or ""),
        "day_columns": ["date", "actual", "predicted"],
    }


# --- pipeline page ------------------------------------------------------------------------------


def build_pipeline(conn, now):
    runs = rows(conn, """
        SELECT TOP (30) run_id, mode, target, trigger_name, status, started_at, ended_at, duration_s, rows_read,
               rows_written, rows_inserted, rows_updated, items_planned, error_message
        FROM etl.pipeline_run ORDER BY run_id DESC
    """)
    freshness = rows(conn, """
        SELECT 'naps' AS source, MAX(hour_utc) AS latest_utc, COUNT_BIG(*) AS rows
        FROM core.measurement WHERE source = 'naps'
        UNION ALL SELECT 'openaq', MAX(hour_utc), COUNT_BIG(*) FROM core.measurement WHERE source = 'openaq'
        UNION ALL SELECT 'eccc climate', MAX(hour_utc), COUNT_BIG(*) FROM core.weather_hourly
        UNION ALL SELECT 'open-meteo', MAX(IIF(kind = 'hist', hour_utc, NULL)), COUNT_BIG(*)
        FROM core.weather_forecast_hourly
        UNION ALL SELECT 'eccc aqhi obs', MAX(observation_utc), COUNT_BIG(*) FROM core.aqhi_obs
        UNION ALL SELECT 'eccc aqhi forecasts', MAX(publication_utc), COUNT_BIG(*) FROM core.aqhi_forecast
    """)
    last_id = runs[0]["run_id"] if runs else None
    dq = rows(conn, "SELECT check_name, severity, status, observed, threshold, detail FROM etl.dq_result "
                    "WHERE run_id = (SELECT MAX(run_id) FROM etl.dq_result WHERE check_name <> 'type_conversion') "
                    "AND check_name <> 'type_conversion' ORDER BY dq_result_id")
    stages = rows(conn, "SELECT source_id, COUNT(*) AS items, SUM(rows_read) AS rows_read, SUM(seconds) AS seconds "
                        "FROM etl.stage_run WHERE run_id = ? AND stage = 'extract' GROUP BY source_id", last_id)
    counts = rows(conn, """
        SELECT 'measurement' AS tbl, COUNT_BIG(*) AS n FROM core.measurement
        UNION ALL SELECT 'weather_hourly', COUNT_BIG(*) FROM core.weather_hourly
        UNION ALL SELECT 'weather_forecast_hourly', COUNT_BIG(*) FROM core.weather_forecast_hourly
        UNION ALL SELECT 'station', COUNT_BIG(*) FROM core.station
        UNION ALL SELECT 'daily_station', COUNT_BIG(*) FROM mart.daily_station
    """)
    return {
        "generated_utc": iso(now),
        "runs": [{"run_id": r["run_id"], "mode": r["mode"], "target": r["target"], "trigger": r["trigger_name"],
                  "status": r["status"], "started_utc": iso(r["started_at"]), "ended_utc": iso(r["ended_at"]),
                  "duration_s": num(r["duration_s"], 1), "rows_read": r["rows_read"], "rows_written": r["rows_written"],
                  "rows_inserted": r["rows_inserted"], "rows_updated": r["rows_updated"], "items": r["items_planned"],
                  "error": (r["error_message"] or "")[:300] or None} for r in runs],
        "freshness": [{"source": r["source"], "latest_utc": iso(r["latest_utc"]), "rows": r["rows"]}
                      for r in freshness],
        "dq": [{"check": r["check_name"], "severity": r["severity"], "status": r["status"],
                "observed": num(r["observed"], 3), "threshold": num(r["threshold"], 3), "detail": r["detail"]}
               for r in dq],
        "last_run_sources": [{"source": r["source_id"], "items": r["items"], "rows_read": r["rows_read"],
                              "seconds": num(r["seconds"], 1)} for r in stages],
        "row_counts": {r["tbl"]: r["n"] for r in counts},
    }


# --- write --------------------------------------------------------------------------------------


def validate(name, payload):
    missing = [k for k in SCHEMA[name] if k not in payload]
    if missing:
        raise ValueError(f"{name} is missing {missing}")
    return True


def run(run_id=None, local_copy=None):
    now = datetime.now(UTC).replace(microsecond=0)
    store = storage.get_storage()
    with db.connect() as conn:
        stations = build_stations(conn, now)
        files = {
            "stations.json": stations,
            "summary.json": build_summary(conn, now, stations),
            "pipeline.json": build_pipeline(conn, now),
        }
    forecast = build_forecast(store)
    if forecast:
        files["forecast.json"] = forecast
    local_copy = (config.STORAGE_MODE == "local") if local_copy is None else local_copy
    written = []
    for name, payload in files.items():
        validate(name, payload)
        data = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        store.write_bytes(f"publish/{name}", data)
        if local_copy:
            FRONTEND_DATA.mkdir(parents=True, exist_ok=True)
            (FRONTEND_DATA / name).write_bytes(data)
        written.append({"file": f"publish/{name}", "bytes": len(data)})
    total = sum(w["bytes"] for w in written)
    print(f"published {len(written)} files, {total / 1024:.0f} KB total")
    return written


def main(argv=None):
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
