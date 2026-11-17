"""
make_fixtures.py

Cuts small real samples from each source into tests/fixtures/, picking the awkward cases on purpose:
naps sentinels, odd negatives and two method codes, the o3 file without a method column, openaq ppm values,
geomet nulls and calm wind, and open-meteo hours with no boundary layer height. Run it with DEV_CACHE=1.
"""

import csv
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from pipeline import geomet_engine, naps_engine, openaq_engine, openmeteo_engine

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
HOURLY = naps_engine.hourly_dir(2024)


def naps_file(name):
    return naps_engine.download(f"{HOURLY}/{name}")["content"].decode("utf-8-sig")


def cut_naps(text, keep):
    """Keeps the 8 preamble/header lines, then rows chosen by keep(row, header)."""
    lines = text.splitlines(keepends=True)
    header = next(csv.reader([lines[7]]))
    picked = [line for line in lines[8:] if keep(next(csv.reader([line])), header)]
    return "".join(lines[:8] + picked)


def naps_samples():
    pm = naps_file("PM25_2024.csv")
    seen = {"sentinel": 0, "negative": 0, "oshawa": 0, "plain": 0}

    def keep_pm(row, header):
        # a few rows of each awkward kind, all from stations inside the region
        hours = row[8:32]
        kind = None
        if "-999" in hours and row[2] == "060410":
            kind = "sentinel"
        elif any(h.startswith("-") and h != "-999" for h in hours):
            kind = "negative"
        elif row[2] == "061703" and row[7] in ("2024-03-10", "2024-03-11"):
            kind = "oshawa"  # two method codes for the same station-day
        elif row[2] == "060410" and row[7] == "2024-07-01":
            kind = "plain"
        elif row[4] == "QC" and row[7] == "2024-01-01" and seen["plain"] == 0:
            kind = "plain"  # one row outside the region, to prove it gets skipped
        if kind and seen[kind] < 2:
            seen[kind] += 1
            return True
        return False

    (OUT / "naps_pm25_2024_sample.csv").write_text(cut_naps(pm, keep_pm), encoding="utf-8-sig")
    o3 = naps_file("O3_2024.csv")
    (OUT / "naps_o3_2024_sample.csv").write_text(
        # the o3 file writes naps ids without the leading zero ("60410")
        cut_naps(o3, lambda row, h: row[1].zfill(6) == "060410" and row[6] in ("2024-01-01", "2024-07-01")),
        encoding="utf-8-sig")

    stations = naps_engine.download(naps_engine.STATION_LIST)["content"].decode("utf-8-sig").splitlines(True)
    start = next(i for i, line in enumerate(stations) if line.startswith('"**********Stations'))
    end = next(i for i in range(start + 2, len(stations)) if stations[i].startswith('"*'))
    wanted = ('"060410"', '"061703"', '"010101"', '"060445"')
    body = [line for line in stations[start + 2:end] if line.startswith(wanted)]
    sample = stations[:3] + stations[start:start + 2] + body + stations[end:end + 3]
    (OUT / "naps_stations_sample.csv").write_text("".join(sample), encoding="utf-8-sig")


def openaq_samples():
    locs = openaq_engine.locations()
    monitor = next(loc for loc in locs if loc["isMonitor"] and loc["name"] == "Toronto West")
    low_cost = next(loc for loc in locs if not loc["isMonitor"])
    (OUT / "openaq_locations_sample.json").write_text(json.dumps([monitor, low_cost], indent=1), encoding="utf-8")
    o3 = next(s for s in monitor["sensors"] if s["parameter"]["name"] == "o3")
    start, end = datetime(2025, 7, 1, tzinfo=UTC), datetime(2025, 7, 1, 6, tzinfo=UTC)
    rows, _ = openaq_engine.sensor_hours(o3["id"], start, end)
    sample = {"sensor_id": o3["id"], "location_id": monitor["id"], "results": rows}
    (OUT / "openaq_hours_o3_sample.json").write_text(json.dumps(sample, indent=1), encoding="utf-8")


def geomet_samples():
    feats = geomet_engine.climate_hourly("6158731", datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 7, 2, tzinfo=UTC))
    calm = [f for f in feats if f["properties"]["WIND_DIRECTION"] in (0, None)][:2]
    (OUT / "geomet_climate_hourly_sample.json").write_text(json.dumps(feats[:4] + calm, indent=1), encoding="utf-8")
    fc = geomet_engine.aqhi_forecasts()
    hourly = next(f for f in fc if "forecast_datetime" in f["properties"])
    period = next(f for f in fc if "forecast_period" in f["properties"])
    (OUT / "geomet_aqhi_forecast_sample.json").write_text(json.dumps([hourly, period], indent=1), encoding="utf-8")
    stations = geomet_engine.aqhi_stations()[:3]
    (OUT / "geomet_aqhi_stations_sample.json").write_text(json.dumps(stations, indent=1), encoding="utf-8")


def openmeteo_samples():
    points = [{"point_id": "43.7,-79.4", "lat": 43.7, "lon": -79.4},
              {"point_id": "43.2,-79.8", "lat": 43.2, "lon": -79.8}]
    pairs = openmeteo_engine.historical(points, date(2024, 6, 1), date(2024, 6, 1))
    sample = [{"point": p, "payload": body} for p, body in pairs]
    (OUT / "openmeteo_hist_sample.json").write_text(json.dumps(sample, indent=1), encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    naps_samples()
    openaq_samples()
    geomet_samples()
    openmeteo_samples()
    for f in sorted(OUT.iterdir()):
        print(f"{f.name:40} {f.stat().st_size:>8} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

