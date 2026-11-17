"""
plan.py

Turns watermarks into a list of work items (source x station batch x time window), each small enough
to finish well inside the 230 s Azure Function limit. run.py and /api/plan both call build_plan().
"""

import math
from datetime import UTC, date, datetime, timedelta

from pipeline import clean_engine, config, geomet_engine, naps_engine, openaq_engine, openmeteo_engine, sources
from pipeline import watermark as wm

OPENAQ_ITEM_SECONDS = 150  # leaves headroom under the 200 s budget per function call
OPENAQ_INCREMENTAL_SENSORS = 10
OPENMETEO_FAIR_USE_PER_DAY = 10000


def _item(source_id, item_id, params, est_requests):
    return {"source": source_id, "item_id": item_id, "params": params, "est_requests": est_requests}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(text):
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


# --- discovery -------------------------------------------------------------------------------


def discover(now, start, notes):
    """Asks every source what exists in the region. A handful of requests in total."""
    ctx = {"naps_stations": [], "naps_files": [], "naps_last_year": None, "aqhi_stations": [],
           "climate_stations": [], "openaq_locations": [], "station_list": None}

    ctx["naps_last_year"] = naps_engine.latest_hourly_year(start.year, now.year)
    if ctx["naps_last_year"]:
        years = range(start.year, ctx["naps_last_year"] + 1)
        ctx["naps_files"] = naps_engine.discover_hourly_files(years, config.POLLUTANTS)
    else:
        notes.append("naps: no continuous hourly files found in the window")

    ctx["station_list"] = naps_engine.station_list_info()
    station_file = naps_engine.download(ctx["station_list"]["path"])
    stations = clean_engine.parse_naps_station_list(station_file["content"])
    ctx["naps_stations"] = clean_engine.naps_stations_in_bbox(
        stations, config.REGION_BBOX, config.POLLUTANTS, open_since=start.strftime("%Y-%m-%d")
    )

    ctx["aqhi_stations"] = geomet_engine.aqhi_stations()
    ctx["climate_stations"] = geomet_engine.climate_stations(active_since=start)

    if config.OPENAQ_API_KEY:
        ctx["openaq_locations"] = openaq_engine.locations()
    else:
        notes.append("openaq: OPENAQ_API_KEY not set, openaq items skipped")
    return ctx


def openaq_sensors(locations, start, handover, now):
    """Sensors we want, each with the window it needs: after naps ends, inside the training window."""
    wanted = set(config.POLLUTANTS)
    sensors = []
    for loc in locations:
        low_cost = not loc.get("isMonitor", False)
        if low_cost and not config.INCLUDE_LOW_COST_SENSORS:
            continue
        first = _parse_utc((loc.get("datetimeFirst") or {}).get("utc"))
        last = _parse_utc((loc.get("datetimeLast") or {}).get("utc"))
        for sensor in loc.get("sensors", []):
            pollutant = sensor.get("parameter", {}).get("name")
            if pollutant not in wanted:
                continue
            sensors.append({
                "sensor_id": sensor["id"],
                "location_id": loc["id"],
                "pollutant": pollutant,
                "low_cost": low_cost,
                "first": first,
                "last": last,
            })
    return sensors


def weather_points(ctx):
    """Unique forecast points from every air station's coordinates, snapped to the ~10 km grid."""
    coords = []
    for s in ctx["naps_stations"]:
        coords.append((float(s["Latitude"]), float(s["Longitude"])))
    for f in ctx["aqhi_stations"]:
        lon, lat = f["geometry"]["coordinates"][:2]
        coords.append((lat, lon))
    for loc in ctx["openaq_locations"]:
        c = loc.get("coordinates") or {}
        if c.get("latitude") is not None:
            if not loc.get("isMonitor", False) and not config.INCLUDE_LOW_COST_SENSORS:
                continue
            coords.append((c["latitude"], c["longitude"]))
    points = {}
    for lat, lon in coords:
        pid = openmeteo_engine.point_id(lat, lon)
        snapped = {"lat": openmeteo_engine.snap(lat), "lon": openmeteo_engine.snap(lon)}
        points.setdefault(pid, {"point_id": pid, **snapped})
    return sorted(points.values(), key=lambda p: p["point_id"])


# --- per source plans -------------------------------------------------------------------------


def plan_naps(mode, ctx, state):
    items = []
    info = ctx["station_list"]
    if mode == "full" or state["source"].get(("naps_stations", info["path"])) != info["last_modified"]:
        items.append(_item("naps_stations", "naps_stations", {"path": info["path"],
                                                              "last_modified": info["last_modified"]}, 1))
    for f in ctx["naps_files"]:
        if mode == "incremental" and state["source"].get(("naps_hourly", f["path"])) == f["last_modified"]:
            continue  # unchanged since we loaded it
        items.append(_item("naps_hourly", f"naps_hourly:{f['pollutant']}:{f['year']}",
                           {"path": f["path"], "pollutant": f["pollutant"], "year": f["year"],
                            "last_modified": f["last_modified"],
                            "etag": state["source"].get(("naps_hourly_etag", f["path"]))}, 1))
    return items


def plan_geomet(mode, ctx, state, start, now):
    items = [_item("aqhi_stations", "aqhi_stations", {}, 1),
             _item("climate_stations", "climate_stations", {"active_since": _iso(start)}, 1)]
    for source_id in ("aqhi_obs", "aqhi_forecast"):
        since = None
        if mode == "incremental":
            since = wm.incremental_from(state["source"].get((source_id, "max_utc")))
        items.append(_item(source_id, source_id, {"since": _iso(since) if since else None}, 1))

    for f in ctx["climate_stations"]:
        p = f["properties"]
        cid = p["CLIMATE_IDENTIFIER"]
        first = _parse_utc((p.get("HLY_FIRST_DATE") or "").replace(" ", "T") + "Z") or start
        station_start = max(start, first)
        last = state["source"].get(("climate_hourly", cid))
        if mode == "incremental" and last:
            windows = [(wm.incremental_from(last, floor=station_start), now)]
        else:
            # one calendar year per item keeps each one to a single 10k page
            windows = []
            year = station_start.year
            while year <= now.year:
                lo = max(station_start, datetime(year, 1, 1, tzinfo=UTC))
                hi = min(now, datetime(year + 1, 1, 1, tzinfo=UTC))
                windows.append((lo, hi))
                year += 1
        for lo, hi in windows:
            items.append(_item("climate_hourly", f"climate_hourly:{cid}:{lo:%Y%m%d}",
                               {"climate_id": cid, "from": _iso(lo), "to": _iso(hi)}, 1))
    return items


def plan_openaq(mode, ctx, state, start, handover, now):
    sensors = openaq_sensors(ctx["openaq_locations"], start, handover, now)
    budget = max(1, OPENAQ_ITEM_SECONDS * config.OPENAQ_RPM // 60)  # requests per item
    segments = []
    for s in sensors:
        floor = max(start, handover, s["first"] or start)
        last_loaded = state["sensor"].get(s["sensor_id"])
        lo = wm.incremental_from(last_loaded, floor=floor) if (mode == "incremental" and last_loaded) else floor
        hi = min(now, (s["last"] + timedelta(hours=1)) if s["last"] else now)
        if hi <= lo:
            continue  # sensor stopped before the window we need
        hours = (hi - lo).total_seconds() / 3600
        # split long windows so no single segment needs more pages than one item can fetch
        pages = openaq_engine.estimate_requests(hours)
        chunks = math.ceil(pages / budget)
        step = (hi - lo) / chunks
        for i in range(chunks):
            a, b = lo + step * i, (lo + step * (i + 1)) if i < chunks - 1 else hi
            a = a.replace(minute=0, second=0, microsecond=0)
            segments.append({"sensor_id": s["sensor_id"], "pollutant": s["pollutant"], "low_cost": s["low_cost"],
                             "from": _iso(a), "to": _iso(b),
                             "est_requests": openaq_engine.estimate_requests((b - a).total_seconds() / 3600)})

    # pack segments into items under the request budget (and a sensor cap for small incremental pulls)
    items, current, used = [], [], 0
    for seg in segments:
        if current and (used + seg["est_requests"] > budget or len(current) >= OPENAQ_INCREMENTAL_SENSORS):
            items.append(current)
            current, used = [], 0
        current.append(seg)
        used += seg["est_requests"]
    if current:
        items.append(current)
    head = [_item("openaq_locations", "openaq_locations", {}, 1)] if ctx["openaq_locations"] else []
    return head + [
        _item("openaq_hours", f"openaq_hours:{i:04d}", {"segments": segs}, sum(s["est_requests"] for s in segs))
        for i, segs in enumerate(items)
    ]


def plan_openmeteo(mode, ctx, state, start, now):
    points = weather_points(ctx)
    yesterday = now.date() - timedelta(days=1)
    items = []
    # group points by the first date they need, then batch them
    needs = {}
    for p in points:
        last = state["source"].get(("openmeteo_hist", p["point_id"]))
        if mode == "incremental" and last:
            first = min(_as_date(last) - timedelta(days=1), yesterday - timedelta(days=1))
        else:
            first = start.date()
        needs.setdefault(first, []).append(p)
    size = openmeteo_engine.MAX_POINTS_PER_CALL
    for first, group in sorted(needs.items()):
        for b in range(0, len(group), size):
            batch = group[b:b + size]
            lo = first
            while lo <= yesterday:
                hi = min(date(lo.year, 12, 31), yesterday)
                weight = len(batch) * math.ceil(((hi - lo).days + 1) / 14) * math.ceil(
                    len(openmeteo_engine.HOURLY_VARS) / 10)
                items.append(_item("openmeteo_hist", f"openmeteo_hist:{first:%Y%m%d}:{b // size}:{lo:%Y}",
                                   {"points": batch, "start_date": lo.isoformat(), "end_date": hi.isoformat()},
                                   weight))
                lo = hi + timedelta(days=1)
    for b in range(0, len(points), size):
        batch = points[b:b + size]
        # 5 days and 7 variables count as one call per point under open-meteo's weighting
        items.append(_item("openmeteo_live", f"openmeteo_live:{b // size}", {"points": batch}, len(batch)))
    return items, points


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def openaq_minutes(requests, rpm=None):
    """Runs at rpm until the hourly cap is used up, then waits out the rest of that hour."""
    rpm = rpm or config.OPENAQ_RPM
    cap = openaq_engine.HOURLY_CAP - openaq_engine.HOURLY_MARGIN
    full_hours, rest = divmod(requests, cap)
    return round(full_hours * 60 + rest / rpm, 1)


# --- the whole plan ---------------------------------------------------------------------------


def build_plan(mode="incremental", state=None, now=None):
    if mode not in ("full", "incremental"):
        raise ValueError(f"mode must be full or incremental, got {mode!r}")
    now = now or wm.now_hour()
    start = wm.window_start(now)
    state = state if state is not None else wm.load_state()
    notes = [state["note"]] if state.get("note") else []

    ctx = discover(now, start, notes)
    # naps hourly is in local standard time (utc-5 here), so its last year ends at 05:00 utc on jan 1
    handover = (datetime(ctx["naps_last_year"] + 1, 1, 1, 5, tzinfo=UTC)
                if ctx["naps_last_year"] else start)

    items = plan_naps(mode, ctx, state)
    items += plan_geomet(mode, ctx, state, start, now)
    items += plan_openaq(mode, ctx, state, start, handover, now)
    om_items, points = plan_openmeteo(mode, ctx, state, start, now)
    items += om_items

    openaq_requests = sum(i["est_requests"] for i in items if i["source"].startswith("openaq"))
    low_cost = sum(1 for loc in ctx["openaq_locations"] if not loc.get("isMonitor", False))
    return {
        "mode": mode,
        "generated_utc": _iso(datetime.now(UTC)),
        "window_start_utc": _iso(start),
        "naps_last_year": ctx["naps_last_year"],
        "handover_utc": _iso(handover),
        "stations": {
            "naps": len(ctx["naps_stations"]),
            "aqhi": len(ctx["aqhi_stations"]),
            "climate": len(ctx["climate_stations"]),
            "openaq_locations": len(ctx["openaq_locations"]),
            "openaq_low_cost_locations": low_cost,
            "weather_points": len(points),
        },
        "state": {"available": state.get("available", False), "sources": len(state["source"]),
                  "sensors": len(state["sensor"])},
        "items": items,
        "sequential_sources": sources.sequential_ids(),
        "estimates": {
            "openaq_requests": openaq_requests,
            "openaq_minutes": openaq_minutes(openaq_requests),
            "openmeteo_weighted_calls": sum(i["est_requests"] for i in items
                                            if i["source"].startswith("openmeteo")),
            "openmeteo_fair_use_per_day": OPENMETEO_FAIR_USE_PER_DAY,
        },
        "notes": notes,
    }
