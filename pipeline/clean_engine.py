"""
clean_engine.py

Python cleaning for every source: reshaping, units, sentinels and time zones, all ending up as UTC rows.
SQL does the set work afterwards (dedupe, merge, matching).
"""

import csv
import io

NAPS_STATION_SECTION = "**********Stations"


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
