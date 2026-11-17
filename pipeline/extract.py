"""
extract.py

One work item in, one clean landing csv out: fetch with the source's engine, clean with clean_engine,
write to landing/<source>/<run_id>/<item>.csv. Both run.py and the function app's /api/extract call this.
"""

import io
import re
import time
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from pipeline import clean_engine as ce
from pipeline import config, geomet_engine, naps_engine, openaq_engine, openmeteo_engine, storage
from pipeline import watermark as wm

FUNCTION_BUDGET_SECONDS = 170  # azure functions called from adf must answer in ~230 s


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC) if text else None


def landing_path(source_id, run_id, item_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", item_id)
    return f"landing/{source_id}/{run_id}/{safe}.csv"


@lru_cache(maxsize=1)
def naps_offsets():
    """naps_id -> hours from utc, from the station list (one small download per process)."""
    info = naps_engine.station_list_info()
    stations = ce.parse_naps_station_list(naps_engine.download(info["path"])["content"])
    out = {}
    for s in stations:
        try:
            out[s["NAPS_ID"].zfill(6)] = float(s["Timezone_UTC"])
        except (KeyError, ValueError):
            continue
    return out


def _source_wm(source_id, key, text=None, utc=None):
    return {"source_id": source_id, "state_key": key, "value_text": text, "value_utc": utc}


# --- one function per source: returns (header, rows iterator, stats, watermarks) ------------------


def _naps_stations(item):
    p = item["params"]
    got = naps_engine.download(p["path"])
    stations = ce.parse_naps_station_list(got["content"])
    keep = ce.naps_stations_in_bbox(stations, config.REGION_BBOX, config.POLLUTANTS,
                                    open_since=wm.window_start().strftime("%Y-%m-%d"))
    stats = {"rows_read": len(stations)}
    marks = {"source_state": [_source_wm("naps_stations", p["path"], text=p.get("last_modified"))]}
    return ce.NAPS_STATION_HEADER, ce.clean_naps_stations(keep, item["item_id"]), stats, marks


def _naps_hourly(item):
    p = item["params"]
    got = naps_engine.download(p["path"], etag=p.get("etag"))
    stats = {"rows_read": 0, "origin": got["origin"]}
    marks = {"source_state": [_source_wm("naps_hourly", p["path"], text=p.get("last_modified"))]}
    if got.get("etag"):
        marks["source_state"].append(_source_wm("naps_hourly_etag", p["path"], text=got["etag"]))
    if got["content"] is None or (p.get("sha256") and p["sha256"] == got["sha256"]):
        stats["unchanged"] = True
        return ce.MEASUREMENT_HEADER, iter(()), stats, marks
    marks["source_state"].append(_source_wm("naps_hourly_sha256", p["path"], text=got["sha256"]))
    lines = io.StringIO(got["content"].decode("utf-8-sig"))
    rows = ce.naps_hourly_rows(lines, p["pollutant"], naps_offsets(), config.REGION_BBOX, wm.window_start(),
                               item["item_id"], stats)
    return ce.MEASUREMENT_HEADER, rows, stats, marks


def _aqhi_stations(item):
    features = geomet_engine.aqhi_stations()
    return ce.AQHI_STATION_HEADER, ce.clean_aqhi_stations(features, item["item_id"]), \
        {"rows_read": len(features)}, {}


def _max_mark(source_id, values):
    values = [v for v in values if v]
    return {"source_state": [_source_wm(source_id, "max_utc", utc=max(values))]} if values else {}


def _aqhi_obs(item):
    features = geomet_engine.aqhi_observations(since=_utc(item["params"].get("since")))
    # only hours that actually had a value move the watermark, so a late value still gets picked up
    marks = _max_mark("aqhi_obs", [f["properties"].get("observation_datetime") for f in features
                                   if f["properties"].get("aqhi") is not None])
    return ce.AQHI_OBS_HEADER, ce.clean_aqhi_obs(features, item["item_id"]), {"rows_read": len(features)}, marks


def _aqhi_forecast(item):
    features = geomet_engine.aqhi_forecasts(since=_utc(item["params"].get("since")))
    marks = _max_mark("aqhi_forecast", [f["properties"].get("publication_datetime") for f in features])
    return ce.AQHI_FORECAST_HEADER, ce.clean_aqhi_forecasts(features, item["item_id"]), \
        {"rows_read": len(features)}, marks


def _climate_stations(item):
    features = geomet_engine.climate_stations(active_since=_utc(item["params"]["active_since"]))
    return ce.CLIMATE_STATION_HEADER, ce.clean_climate_stations(features, item["item_id"]), \
        {"rows_read": len(features)}, {}


def _climate_hourly(item):
    p = item["params"]
    # the api filters on local time, so pad a day each side; the merge dedupes on utc
    start, end = _utc(p["from"]), _utc(p["to"])
    features = geomet_engine.climate_hourly(p["climate_id"], start - timedelta(days=1), end + timedelta(days=1))
    rows = list(ce.clean_climate_hourly(features, item["item_id"]))
    in_window = [r for r in rows if p["from"] <= r[2] <= p["to"]]
    marks = {}
    if in_window:
        marks = {"source_state": [_source_wm("climate_hourly", p["climate_id"], utc=max(r[2] for r in in_window))]}
    return ce.CLIMATE_HOURLY_HEADER, iter(in_window), {"rows_read": len(features)}, marks


def _openaq_locations(item):
    locations = openaq_engine.locations()
    return ce.OPENAQ_LOCATION_HEADER, ce.clean_openaq_locations(locations, config.POLLUTANTS, item["item_id"]), \
        {"rows_read": len(locations)}, {}


def _openaq_hours(item, deadline):
    stats, sensor_marks, all_rows = {"rows_read": 0}, [], []
    for seg in item["params"]["segments"]:
        if time.monotonic() > deadline:
            stats["stopped_early"] = True
            break
        results, finished = openaq_engine.sensor_hours(seg["sensor_id"], _utc(seg["from"]), _utc(seg["to"]),
                                                       deadline=deadline)
        rows = list(ce.openaq_hour_rows(results, seg["sensor_id"], seg["location_id"], seg["pollutant"],
                                        item["item_id"], stats))
        all_rows.extend(rows)
        sensor_marks.append({
            "sensor_id": seg["sensor_id"], "from": seg["from"], "to": seg["to"], "finished": finished,
            "first_segment": seg.get("first_segment", False),
            "last_hour_utc": max((r[5] for r in rows), default=None),
        })
        if not finished:
            stats["stopped_early"] = True
            break
    return ce.MEASUREMENT_HEADER, iter(all_rows), stats, {"sensor_state": sensor_marks}


def _openmeteo(item, kind):
    p = item["params"]
    if kind == "hist":
        pairs = openmeteo_engine.historical(p["points"], date.fromisoformat(p["start_date"]),
                                            date.fromisoformat(p["end_date"]))
        marks = {"source_state": [_source_wm("openmeteo_hist", pt["point_id"], text=p["end_date"])
                                  for pt in p["points"]]}
    else:
        pairs = openmeteo_engine.live(p["points"])
        marks = {}
    return ce.WEATHER_FORECAST_HEADER, ce.clean_openmeteo(pairs, kind, item["item_id"]), \
        {"rows_read": sum(len((b.get("hourly") or {}).get("time", [])) for _, b in pairs)}, marks


def extract(run_id, item, store=None, budget_seconds=FUNCTION_BUDGET_SECONDS):
    """Runs one work item. Returns what /api/extract hands back to adf."""
    started = time.monotonic()
    deadline = started + budget_seconds
    source_id = item["source"]
    handlers = {
        "naps_stations": _naps_stations,
        "naps_hourly": _naps_hourly,
        "aqhi_stations": _aqhi_stations,
        "aqhi_obs": _aqhi_obs,
        "aqhi_forecast": _aqhi_forecast,
        "climate_stations": _climate_stations,
        "climate_hourly": _climate_hourly,
        "openaq_locations": _openaq_locations,
        "openaq_hours": lambda it: _openaq_hours(it, deadline),
        "openmeteo_hist": lambda it: _openmeteo(it, "hist"),
        "openmeteo_live": lambda it: _openmeteo(it, "live"),
    }
    if source_id not in handlers:
        raise ValueError(f"unknown source {source_id!r}")
    header, rows, stats, marks = handlers[source_id](item)
    path = landing_path(source_id, run_id, item["item_id"])
    written = storage.write_csv(path, header, rows, storage=store)
    return {
        "source": source_id,
        "item_id": item["item_id"],
        "files": [path],
        "rows_read": stats.get("rows_read", 0),
        "rows_written": written,
        "seconds": round(time.monotonic() - started, 2),
        "new_watermark": marks,
        "stats": stats,
    }
