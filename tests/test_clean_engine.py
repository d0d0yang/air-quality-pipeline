"""
test_clean_engine.py

Every cleaning rule, run on the small real samples in tests/fixtures/.
"""

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline import clean_engine as ce
from pipeline import config

FIX = Path(__file__).parent / "fixtures"
START = datetime(2024, 1, 1, tzinfo=UTC)
WIDE_BBOX = (-141.0, 41.0, -52.0, 84.0)  # all of canada, to keep the rows outside our region
OFFSETS = {"060410": -5.0, "061703": -5.0}


def naps_rows(name, pollutant, bbox=None, stats=None):
    text = (FIX / name).read_text(encoding="utf-8-sig")
    return list(ce.naps_hourly_rows(io.StringIO(text), pollutant, OFFSETS, bbox or config.REGION_BBOX, START,
                                    "item", stats))


def by_station_hour(rows):
    return {(r[2], r[5]): r for r in rows}


# --- naps ---


def test_naps_reshapes_wide_to_long_in_the_region():
    stats = {}
    rows = naps_rows("naps_pm25_2024_sample.csv", "pm25", stats=stats)
    stations = {r[2] for r in rows}
    assert stations == {"060410", "061703"}  # montreal and thunder bay rows are outside the bbox
    assert stats["skipped_outside"] == 2
    # 5 station-days x 24 hours, minus the two -999 hours
    assert len(rows) == 5 * 24 - 2


def test_naps_hour_ending_local_standard_time_becomes_hour_beginning_utc():
    rows = by_station_hour(naps_rows("naps_pm25_2024_sample.csv", "pm25"))
    # H01 on 2024-07-01 is 00:00-01:00 EST, which starts at 05:00 utc (no daylight time in naps files)
    assert rows[("060410", "2024-07-01T05:00:00Z")][6] == 3.0
    # H22 = 25 ug/m3 is 21:00 EST = 02:00 utc the next day
    assert rows[("060410", "2024-07-02T02:00:00Z")][6] == 25.0


def test_naps_sentinel_produces_no_row():
    rows = by_station_hour(naps_rows("naps_pm25_2024_sample.csv", "pm25"))
    # H12 on 2024-01-30 is -999: 11:00 EST = 16:00 utc
    assert ("060410", "2024-01-30T16:00:00Z") not in rows
    assert ("060410", "2024-01-30T15:00:00Z") in rows


def test_naps_other_negatives_become_null_with_flag():
    rows = naps_rows("naps_pm25_2024_sample.csv", "pm25", bbox=WIDE_BBOX)
    negatives = [r for r in rows if r[2] == "060809" and r[10] == "negative"]
    assert negatives and all(r[6] is None for r in negatives)


def test_naps_keeps_method_code_and_zero_pads_ids():
    rows = naps_rows("naps_pm25_2024_sample.csv", "pm25")
    assert {r[8] for r in rows if r[2] == "061703"} == {"184"}
    assert all(len(r[2]) == 6 for r in rows)


def test_naps_o3_file_has_no_method_column():
    rows = naps_rows("naps_o3_2024_sample.csv", "o3")
    assert rows and all(r[8] == "" for r in rows)
    assert all(r[7] == "ppb" for r in rows)
    # the o3 file writes "60410"; it comes out padded like every other file
    assert {r[2] for r in rows} == {"060410"}


def test_naps_drops_days_before_the_window():
    text = (FIX / "naps_pm25_2024_sample.csv").read_text(encoding="utf-8-sig")
    rows = list(ce.naps_hourly_rows(io.StringIO(text), "pm25", OFFSETS, config.REGION_BBOX,
                                    datetime(2024, 6, 1, tzinfo=UTC), "item"))
    assert {r[5][:10] for r in rows} <= {"2024-07-01", "2024-07-02"}


def test_naps_missing_header_raises():
    with pytest.raises(ValueError):
        list(ce.naps_hourly_rows(io.StringIO("a,b\n1,2\n"), "pm25", {}, config.REGION_BBOX, START, "x"))


def test_naps_station_sample_parses():
    stations = ce.parse_naps_station_list((FIX / "naps_stations_sample.csv").read_bytes())
    ids = {s["NAPS_ID"] for s in stations}
    assert {"060410", "061703"} <= ids
    rows = list(ce.clean_naps_stations(stations, "item"))
    assert all(len(r) == len(ce.NAPS_STATION_HEADER) for r in rows)


# --- universal rules ---


def test_check_value_bounds():
    assert ce.check_value("pm25", -1.0) == (None, "negative")
    assert ce.check_value("pm25", 1200.0) == (1200.0, "above_plausible_max")
    assert ce.check_value("pm25", 12.0) == (12.0, "")
    assert ce.check_value("pm25", None) == (None, "")


def test_unit_conversion():
    assert ce.to_canonical("o3", 0.029, "ppm") == pytest.approx(29.0)
    assert ce.to_canonical("pm25", 7.0, "µg/m³") == 7.0
    with pytest.raises(ValueError):
        ce.to_canonical("o3", 1.0, "ug/m3")


# --- openaq ---


def test_openaq_hours_convert_ppm_to_ppb_and_keep_coverage():
    sample = json.loads((FIX / "openaq_hours_o3_sample.json").read_text(encoding="utf-8"))
    rows = list(ce.openaq_hour_rows(sample["results"], sample["sensor_id"], sample["location_id"], "o3", "item"))
    first = rows[0]
    assert first[5] == "2025-07-01T00:00:00Z"  # hour-beginning utc, straight from period.datetimeFrom
    assert first[6] == pytest.approx(29.0)
    assert first[7] == "ppb"
    assert first[9] == 100.0


def test_openaq_hours_drop_exact_duplicates():
    sample = json.loads((FIX / "openaq_hours_o3_sample.json").read_text(encoding="utf-8"))
    stats = {}
    doubled = sample["results"] + sample["results"][:2]
    rows = list(ce.openaq_hour_rows(doubled, 1, 2, "o3", "item", stats))
    assert len(rows) == len(sample["results"])
    assert stats["duplicates"] == 2


def test_openaq_flagged_values_are_tagged():
    rows = list(ce.openaq_hour_rows([{"value": 5, "flagInfo": {"hasFlags": True},
                                      "parameter": {"units": "µg/m³"},
                                      "period": {"datetimeFrom": {"utc": "2025-01-01T00:00:00Z"}}}],
                                    1, 2, "pm25", "item"))
    assert rows[0][10] == "openaq_flagged"


def test_openaq_locations_keep_our_pollutants_and_licences():
    locs = json.loads((FIX / "openaq_locations_sample.json").read_text(encoding="utf-8"))
    rows = list(ce.clean_openaq_locations(locs, ["pm25", "o3", "no2"], "item"))
    by_loc = {}
    for r in rows:
        by_loc.setdefault(r[3], []).append(r)
    assert sorted(r[13] for r in by_loc["Toronto West"]) == ["no2", "o3", "pm25"]
    low_cost = by_loc["Lilac Hill"]
    assert [r[13] for r in low_cost] == ["pm25"] and low_cost[0][8] == 0
    assert all(r[15] for r in rows)  # licence names carried through


# --- geomet ---


def test_climate_hourly_wind_direction_and_utc():
    feats = json.loads((FIX / "geomet_climate_hourly_sample.json").read_text(encoding="utf-8"))
    rows = list(ce.clean_climate_hourly(feats, "item"))
    first = rows[0]
    assert first[2] == "2024-07-01T05:00:00Z"
    assert first[6] == 320  # 32 tens of degrees
    calm = [r for r in rows if r[2] == "2024-07-02T04:00:00Z"][0]
    assert calm[6] is None  # 0 means calm, which has no direction


def test_climate_stations_take_decimal_coordinates_from_geometry():
    # real values for st catharines a: the properties hold packed dms, the geometry holds decimals
    feature = {"geometry": {"type": "Point", "coordinates": [-79.16666666666667, 43.2]},
               "properties": {"CLIMATE_IDENTIFIER": "6137304", "STN_ID": 53000, "STATION_NAME": "ST CATHARINES A",
                              "LATITUDE": 431200000, "LONGITUDE": -791000000, "ELEVATION": 97.8}}
    row = list(ce.clean_climate_stations([feature], "item"))[0]
    assert (row[4], row[5]) == (43.2, -79.16666666666667)


def test_aqhi_forecasts_flatten_both_shapes():
    feats = json.loads((FIX / "geomet_aqhi_forecast_sample.json").read_text(encoding="utf-8"))
    rows = list(ce.clean_aqhi_forecasts(feats, "item"))
    kinds = [r[3] for r in rows]
    assert kinds.count("hourly") == 1
    assert kinds.count("period") == 4
    assert {r[6] for r in rows if r[3] == "period"} >= {"Tomorrow"}


def test_aqhi_stations_have_coordinates():
    feats = json.loads((FIX / "geomet_aqhi_stations_sample.json").read_text(encoding="utf-8"))
    rows = list(ce.clean_aqhi_stations(feats, "item"))
    assert all(42 < r[3] < 45 and -82 < r[4] < -78 for r in rows)


# --- open-meteo ---


def test_openmeteo_long_rows_per_point_with_missing_blh():
    sample = json.loads((FIX / "openmeteo_hist_sample.json").read_text(encoding="utf-8"))
    pairs = [(s["point"], s["payload"]) for s in sample]
    rows = list(ce.clean_openmeteo(pairs, "hist", "item"))
    assert len(rows) == 48
    assert rows[0][1] == "43.7,-79.4" and rows[24][1] == "43.2,-79.8"
    assert rows[0][4] == "2024-06-01T00:00:00Z"
    assert rows[0][6] == 20.5
    assert rows[0][10] is None  # boundary layer height isn't in the archive before late 2024


def test_openmeteo_skips_hours_with_no_values():
    point = {"point_id": "p", "lat": 1, "lon": 2}
    payload = {"hourly": {"time": ["2024-01-01T00:00", "2024-01-01T01:00"], "temperature_2m": [None, 1.0]}}
    rows = list(ce.clean_openmeteo([(point, payload)], "live", "item"))
    assert [r[4] for r in rows] == ["2024-01-01T01:00:00Z"]
