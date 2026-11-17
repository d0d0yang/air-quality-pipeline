"""
clean_engine.py

Python cleaning for every source: reshaping, units, sentinels and time zones, all ending up as UTC rows.
SQL does the set work afterwards (dedupe, merge, matching). Every cleaner is a generator so big files stream.
"""

import csv
import io
from datetime import UTC, datetime, timedelta

NAPS_STATION_SECTION = "**********Stations"
NAPS_SENTINEL = "-999"

# values above these are kept but flagged; they're possible (smoke) but worth a look
PLAUSIBLE_MAX = {"pm25": 1000.0, "pm10": 2000.0, "o3": 500.0, "no2": 1000.0}
UNIT_TO_CANONICAL = {
    ("pm25", "ug/m3"): 1.0, ("pm10", "ug/m3"): 1.0,
    ("o3", "ppb"): 1.0, ("no2", "ppb"): 1.0,
    ("o3", "ppm"): 1000.0, ("no2", "ppm"): 1000.0,
}

MEASUREMENT_HEADER = ["item_id", "source", "station_ref", "sensor_id", "pollutant", "hour_utc", "value",
                      "unit", "method_code", "coverage_pct", "qc_flag"]


def iso_hour(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_unit(unit):
    """'µg/m³', 'ug/m3' and the mangled versions all become 'ug/m3'."""
    u = (unit or "").strip().lower().replace("µ", "u").replace("μ", "u").replace("³", "3").replace(" ", "")
    return u


def check_value(pollutant, value):
    """Universal bounds. Returns (value, flag): negatives become null, very high values get a flag."""
    if value is None:
        return None, ""
    if value < 0:
        return None, "negative"
    if value > PLAUSIBLE_MAX.get(pollutant, float("inf")):
        return value, "above_plausible_max"
    return value, ""


def to_canonical(pollutant, value, unit):
    factor = UNIT_TO_CANONICAL.get((pollutant, normalize_unit(unit)))
    if factor is None:
        raise ValueError(f"no unit conversion for {pollutant} in {unit!r}")
    return None if value is None else value * factor


# --- naps ---------------------------------------------------------------------------------------


def parse_naps_station_list(content):
    """
    The station list csv has several sections; the stations one starts after a
    '**********Stations<year>**********' line and ends at the next '*' line.
    """
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    rows = csv.reader(io.StringIO(text))
    header, stations = None, []
    for row in rows:
        first = row[0].strip() if row else ""
        if header is None:
            if first.startswith(NAPS_STATION_SECTION):
                header = next(rows)
            continue
        if not first or first.startswith("*"):
            break
        stations.append(dict(zip(header, (c.strip() for c in row), strict=False)))
    if header is None:
        raise ValueError("naps station list: stations section not found")
    return stations


def naps_stations_in_bbox(stations, bbox, pollutants, open_since=None):
    """
    Stations inside the bbox that measure one of our pollutants continuously ('X' now, 'H' in the past)
    and hadn't closed before `open_since` (a yyyy-mm-dd string).
    """
    columns = {"pm25": "PM2.5_Continuous", "pm10": "PM10_Continuous", "o3": "O3", "no2": "NO2"}
    lon_min, lat_min, lon_max, lat_max = bbox
    keep = []
    for s in stations:
        try:
            lat, lon = float(s["Latitude"]), float(s["Longitude"])
        except (KeyError, ValueError):
            continue
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            continue
        end_date = s.get("End_Date") or ""
        if open_since and end_date and end_date < open_since:
            continue
        if any(s.get(columns[p]) in ("X", "H") for p in pollutants if p in columns):
            keep.append(s)
    return keep


NAPS_STATION_HEADER = ["item_id", "naps_id", "station_name", "status", "city", "province", "utc_offset_h",
                       "lat", "lon", "elevation_m", "start_date", "end_date", "site_type", "urbanization"]


def clean_naps_stations(stations, item_id):
    for s in stations:
        yield [item_id, s["NAPS_ID"].zfill(6), s.get("Station_Name"), s.get("Status"), s.get("City"),
               s.get("Province/Territory_EN"), s.get("Timezone_UTC"), s.get("Latitude"), s.get("Longitude"),
               s.get("Elevation_m"), s.get("Start_Date"), s.get("End_Date"), s.get("Site_Type"),
               s.get("Urbanization")]


def _naps_header_key(cell):
    # "NAPS ID//Identifiant SNPA" -> "naps id"; the french half and odd spacing are dropped
    return cell.split("//")[0].strip().lower()


def naps_hourly_rows(lines, pollutant, offsets, bbox, start, item_id, stats=None):
    """
    Reshapes one NAPS hourly file (wide, H01..H24 per station-day) into long hourly rows.
    - preamble lines are skipped until the real header; columns are found by name (O3 has no method column)
    - "-999" means no data and produces no row; other negatives become null with a flag
    - times are hour-ending local standard time: H01 on day D is the hour starting at D 00:00 LST,
      so hour_utc = D + (n-1) h - utc_offset (no DST in the files)
    `lines` is any iterable of text lines, so the file streams without loading it whole.
    """
    stats = stats if stats is not None else {}
    stats.setdefault("rows_read", 0)
    stats.setdefault("skipped_outside", 0)
    reader = csv.reader(lines)
    unit, header = None, None
    for row in reader:
        if not row:
            continue
        key = _naps_header_key(row[0])
        if key.startswith("units"):
            unit = row[1].strip() if len(row) > 1 else None
        if key == "pollutant" and len(row) > 8 and any("H01" in c for c in row):
            header = [_naps_header_key(c) for c in row]
            break
    if header is None:
        raise ValueError("naps file: header row not found")
    if unit is None:
        raise ValueError("naps file: units line not found")
    col = {name: i for i, name in enumerate(header)}
    hour_cols = [col[f"h{h:02d}"] for h in range(1, 25)]
    i_id, i_lat, i_lon, i_date = col["naps id"], col["latitude"], col["longitude"], col["date"]
    i_method = col.get("method code")
    lon_min, lat_min, lon_max, lat_max = bbox
    start_day = (start - timedelta(days=1)).date().isoformat()

    for row in reader:
        if len(row) <= hour_cols[-1]:
            continue
        stats["rows_read"] += 1
        try:
            lat, lon = float(row[i_lat]), float(row[i_lon])
        except ValueError:
            continue
        day = row[i_date].strip()
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max) or day < start_day:
            stats["skipped_outside"] += 1
            continue
        naps_id = row[i_id].strip().zfill(6)
        offset = offsets.get(naps_id)
        if offset is None:
            # stations missing from the list are in ontario's bbox, which is utc-5 standard time
            offset = -5.0
            stats["default_offset"] = stats.get("default_offset", 0) + 1
        base = datetime.fromisoformat(day).replace(tzinfo=UTC) - timedelta(hours=offset)
        method = row[i_method].strip() if i_method is not None else ""
        for h, idx in enumerate(hour_cols):
            raw = row[idx].strip()
            if raw in ("", NAPS_SENTINEL):
                continue
            value, flag = check_value(pollutant, to_canonical(pollutant, float(raw), unit))
            yield [item_id, "naps", naps_id, "", pollutant, iso_hour(base + timedelta(hours=h)), value,
                   _unit_for(pollutant), method, "", flag]


def _unit_for(pollutant):
    return "ug/m3" if pollutant.startswith("pm") else "ppb"


# --- openaq -------------------------------------------------------------------------------------

OPENAQ_LOCATION_HEADER = ["item_id", "sensor_id", "location_id", "location_name", "locality", "country",
                          "provider", "owner", "is_monitor", "is_mobile", "lat", "lon", "timezone", "pollutant",
                          "units", "licences", "attribution", "first_utc", "last_utc"]


def clean_openaq_locations(locations, pollutants, item_id):
    for loc in locations:
        coords = loc.get("coordinates") or {}
        licences = loc.get("licenses") or []
        attribution = "; ".join(
            (lic.get("attribution") or {}).get("name") or "" for lic in licences if lic.get("attribution"))
        for sensor in loc.get("sensors", []):
            param = sensor.get("parameter") or {}
            if param.get("name") not in pollutants:
                continue
            yield [item_id, sensor["id"], loc["id"], loc.get("name"), loc.get("locality"),
                   (loc.get("country") or {}).get("code"), (loc.get("provider") or {}).get("name"),
                   (loc.get("owner") or {}).get("name"), int(bool(loc.get("isMonitor"))),
                   int(bool(loc.get("isMobile"))), coords.get("latitude"), coords.get("longitude"),
                   loc.get("timezone"), param.get("name"), normalize_unit(param.get("units")),
                   "; ".join(lic.get("name") or "" for lic in licences), attribution,
                   (loc.get("datetimeFirst") or {}).get("utc"), (loc.get("datetimeLast") or {}).get("utc")]


def openaq_hour_rows(results, sensor_id, location_id, pollutant, item_id, stats=None):
    """Flattens /sensors/{id}/hours results: hour-beginning utc, canonical units, coverage kept, dupes dropped."""
    stats = stats if stats is not None else {}
    seen = set()
    for r in results:
        stats["rows_read"] = stats.get("rows_read", 0) + 1
        hour = ((r.get("period") or {}).get("datetimeFrom") or {}).get("utc")
        if not hour or hour in seen:
            stats["duplicates"] = stats.get("duplicates", 0) + (1 if hour else 0)
            continue
        seen.add(hour)
        unit = (r.get("parameter") or {}).get("units")
        raw = r.get("value")
        value, flag = check_value(pollutant, to_canonical(pollutant, None if raw is None else float(raw), unit))
        if (r.get("flagInfo") or {}).get("hasFlags"):
            flag = flag or "openaq_flagged"
        coverage = (r.get("coverage") or {}).get("percentComplete")
        yield [item_id, "openaq", location_id, sensor_id, pollutant, hour.replace("+00:00", "Z"), value,
               _unit_for(pollutant), "", coverage, flag]


# --- geomet -------------------------------------------------------------------------------------

AQHI_STATION_HEADER = ["item_id", "location_id", "name_en", "lat", "lon", "zone", "naps_ids"]
AQHI_OBS_HEADER = ["item_id", "location_id", "observation_utc", "aqhi"]
AQHI_FORECAST_HEADER = ["item_id", "location_id", "publication_utc", "kind", "slot", "forecast_utc",
                        "period_label", "aqhi"]
CLIMATE_STATION_HEADER = ["item_id", "climate_id", "stn_id", "station_name", "lat", "lon", "elevation_m",
                          "hly_first", "hly_last"]
CLIMATE_HOURLY_HEADER = ["item_id", "climate_id", "hour_utc", "temp_c", "rh_pct", "wind_kmh", "wind_dir_deg",
                         "pressure_kpa", "precip_mm", "flags"]


def clean_aqhi_stations(features, item_id):
    for f in features:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        naps = [p.get(f"station.{i}.napsid") for i in range(5)]
        yield [item_id, p["location_id"], p.get("location_name_en"), lat, lon,
               p.get("eccc_administrative-zone_name_en"), ";".join(str(n) for n in naps if n)]


def clean_aqhi_obs(features, item_id):
    for f in features:
        p = f["properties"]
        if p.get("aqhi") is None or not p.get("observation_datetime"):
            continue
        yield [item_id, p["location_id"], p["observation_datetime"], p["aqhi"]]


def clean_aqhi_forecasts(features, item_id):
    """Two shapes: hourly forecasts, and period forecasts (Tonight / Tomorrow / ...) packed in one feature."""
    for f in features:
        p = f["properties"]
        pub = p.get("publication_datetime")
        if "forecast_period" in p:
            for slot, period in sorted((p.get("forecast_period") or {}).items()):
                if not period or period.get("aqhi") is None:
                    continue
                yield [item_id, p["location_id"], pub, "period", slot, "", period.get("forecast_period_en"),
                       period["aqhi"]]
        elif p.get("forecast_datetime") and p.get("aqhi") is not None:
            yield [item_id, p["location_id"], pub, "hourly", p["forecast_datetime"], p["forecast_datetime"], "",
                   p["aqhi"]]


def clean_climate_stations(features, item_id):
    # LATITUDE/LONGITUDE in this collection are packed degrees-minutes-seconds (431500000 = 43°15'00"),
    # so the decimal coordinates come from the geometry instead
    for f in features:
        p = f["properties"]
        lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
        yield [item_id, p["CLIMATE_IDENTIFIER"], p.get("STN_ID"), p.get("STATION_NAME"), lat, lon,
               p.get("ELEVATION"), p.get("HLY_FIRST_DATE"), p.get("HLY_LAST_DATE")]


def _wind_direction(tens_of_degrees):
    # eccc reports tens of degrees (1..36); 0 means calm, which has no direction
    if tens_of_degrees in (None, 0):
        return None
    return (tens_of_degrees * 10) % 360


def clean_climate_hourly(features, item_id):
    for f in features:
        p = f["properties"]
        if not p.get("UTC_DATE"):
            continue
        flags = ";".join(f"{k[:-5].lower()}:{v}" for k, v in sorted(p.items()) if k.endswith("_FLAG") and v)
        yield [item_id, p["CLIMATE_IDENTIFIER"], p["UTC_DATE"][:19] + "Z", p.get("TEMP"), p.get("RELATIVE_HUMIDITY"),
               p.get("WIND_SPEED"), _wind_direction(p.get("WIND_DIRECTION")), p.get("STATION_PRESSURE"),
               p.get("PRECIP_AMOUNT"), flags]


# --- open-meteo ---------------------------------------------------------------------------------

WEATHER_FORECAST_HEADER = ["item_id", "point_id", "lat", "lon", "hour_utc", "kind", "temp_c", "rh_pct",
                           "wind_kmh", "wind_dir_deg", "blh_m", "precip_mm", "cloud_pct"]
OPENMETEO_VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m",
                  "boundary_layer_height", "precipitation", "cloud_cover"]


def clean_openmeteo(pairs, kind, item_id):
    """(point, payload) pairs from openmeteo_engine -> one row per point per hour."""
    for point, payload in pairs:
        hourly = payload.get("hourly") or {}
        times = hourly.get("time", [])
        columns = [hourly.get(v) or [None] * len(times) for v in OPENMETEO_VARS]
        for i, t in enumerate(times):
            values = [c[i] for c in columns]
            if all(v is None for v in values):
                continue
            yield [item_id, point["point_id"], point["lat"], point["lon"], t + ":00Z" if len(t) == 16 else t, kind,
                   *values]
