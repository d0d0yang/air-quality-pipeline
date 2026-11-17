"""
check_aqhi.py

Checks pipeline/aqhi.py against ECCC: for every AQHI location with a same-named OpenAQ government
monitor within 10 km (aqhi locations are community points, not monitor sites), recompute the hourly
AQHI from that monitor's NO2/O3/PM2.5 and compare with aqhi-observations-realtime over the hours
both have. Prints the mean absolute difference for both readings of ECCC's timestamp.
"""

import difflib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from pipeline import aqhi, clean_engine, config, geomet_engine, openaq_engine

MAX_LINK_M = 10000
MIN_NAME_RATIO = 0.85


def name_key(name):
    return re.sub(r"[^a-z]", "", (name or "").lower())


def same_name(a, b):
    return difflib.SequenceMatcher(None, name_key(a), name_key(b)).ratio() >= MIN_NAME_RATIO


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def hour_grid(first, last):
    """Every hour from first to last inclusive, so gaps show up as missing values."""
    return [first + timedelta(hours=i) for i in range(int((last - first).total_seconds() // 3600) + 1)]


def hourly_series(sensor_id, location_id, pollutant, start, end):
    results, _ = openaq_engine.sensor_hours(sensor_id, start, end)
    rows = clean_engine.openaq_hour_rows(results, sensor_id, location_id, pollutant, "check")
    return {r[5]: r[6] for r in rows}


def main():
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    obs = geomet_engine.aqhi_observations()
    official = defaultdict(dict)
    for f in obs:
        p = f["properties"]
        if p.get("aqhi") is not None:
            official[p["location_id"]][p["observation_datetime"]] = p["aqhi"]
    start = min(parse_utc(t) for v in official.values() for t in v)
    start -= timedelta(hours=3)  # the first hour needs a 3-hour average

    recent = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    monitors = [loc for loc in openaq_engine.locations()
                if loc.get("isMonitor") and ((loc.get("datetimeLast") or {}).get("utc") or "") >= recent]
    report = {"pairs": []}
    diffs = {"hour_ending": [], "hour_beginning": []}
    for st in geomet_engine.aqhi_stations():
        lon, lat = st["geometry"]["coordinates"][:2]
        loc_id = st["properties"]["location_id"]
        near = sorted(((haversine_m(lat, lon, m["coordinates"]["latitude"], m["coordinates"]["longitude"]), m)
                       for m in monitors if same_name(m["name"], st["properties"]["location_name_en"])),
                      key=lambda pair: pair[0])
        if not near or near[0][0] > MAX_LINK_M:
            continue
        dist, mon = near[0]
        sensors = {s["parameter"]["name"]: s["id"] for s in mon["sensors"]}
        if not {"no2", "o3", "pm25"} <= sensors.keys():
            continue
        series = {p: hourly_series(sensors[p], mon["id"], p, start, now) for p in ("no2", "o3", "pm25")}
        hours = sorted(set().union(*series.values()))
        grid = hour_grid(parse_utc(hours[0]), parse_utc(hours[-1]))
        keys = [g.strftime("%Y-%m-%dT%H:%M:%SZ") for g in grid]
        avg = {p: aqhi.rolling_3h([series[p].get(k) for k in keys]) for p in series}
        computed = {}
        for i, g in enumerate(grid):
            value = aqhi.aqhi(avg["no2"][i], avg["o3"][i], avg["pm25"][i])
            if value is not None:
                computed[g] = value  # keyed by hour-beginning, like everything we store
        pair_n = 0
        for t, value in official[loc_id].items():
            t_obs = parse_utc(t)
            for convention, t_ours in (("hour_ending", t_obs - timedelta(hours=1)), ("hour_beginning", t_obs)):
                if t_ours in computed:
                    diffs[convention].append(abs(computed[t_ours] - value))
                    if convention == "hour_ending":
                        pair_n += 1
        report["pairs"].append({"aqhi_location": loc_id, "openaq_location": mon["id"], "name": mon["name"],
                                "distance_m": round(dist), "hours_compared": pair_n})

    for convention, d in diffs.items():
        report[convention] = {"n": len(d), "mae": round(sum(d) / len(d), 3) if d else None,
                              "max": round(max(d), 3) if d else None}
    report["window_utc"] = [start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")]
    report["rpm"] = config.OPENAQ_RPM
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
