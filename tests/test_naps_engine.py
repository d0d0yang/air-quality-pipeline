"""
test_naps_engine.py

Portal listing, file discovery, conditional downloads, the manual fallback and the station list parser.
"""

import re

import pytest
import responses

from pipeline import clean_engine, config, http_client, naps_engine

LIST_URL = re.compile(r"https://data-donnees\.az\.ec\.gc\.ca/api/path_contents.*")
FILE_URL = re.compile(r"https://data-donnees\.az\.ec\.gc\.ca/api/file.*")


def entry(name, year, modified="2026-06-24", is_dir=False):
    return {"name": name, "is_directory": is_dir, "last_modified": modified,
            "path": f"{naps_engine.hourly_dir(year)}/{name}"}


@responses.activate
def test_missing_folder_lists_as_empty():
    responses.get(LIST_URL, status=404, json="Path not found.")
    assert naps_engine.list_dir("nope") == []


@responses.activate
def test_discover_keeps_only_wanted_pollutants():
    responses.get(LIST_URL, json={"path_contents": [
        entry("PM25_2024.csv", 2024), entry("O3_2024.csv", 2024), entry("SO2_2024.csv", 2024),
        entry("NO2_2024.csv", 2024), entry("NOX_2024.csv", 2024),
    ]})
    found = naps_engine.discover_hourly_files([2024], ["pm25", "no2"])
    assert sorted(f["pollutant"] for f in found) == ["no2", "pm25"]
    assert all(f["year"] == 2024 for f in found)


@responses.activate
def test_latest_year_skips_years_without_hourly_data():
    responses.get(LIST_URL, status=404, json="Path not found.")  # 2026
    responses.get(LIST_URL, status=404, json="Path not found.")  # 2025
    responses.get(LIST_URL, json={"path_contents": [entry("PM25_2024.csv", 2024)]})
    assert naps_engine.latest_hourly_year(2022, 2026) == 2024


@responses.activate
def test_download_hashes_content_and_reads_etag():
    responses.get(FILE_URL, body=b"abc", headers={"ETag": '"x1"', "Last-Modified": "Wed, 24 Jun 2026"})
    got = naps_engine.download("air/x/PM25_2024.csv")
    assert got["sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert got["etag"] == '"x1"'


@responses.activate
def test_download_not_modified_returns_no_content():
    responses.get(FILE_URL, status=304)
    got = naps_engine.download("air/x/PM25_2024.csv", etag='"x1"')
    assert got["content"] is None
    assert responses.calls[0].request.headers["If-None-Match"] == '"x1"'


@responses.activate
def test_blocked_download_falls_back_to_manual_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_DATA_DIR", tmp_path)
    manual = tmp_path / "manual" / "naps"
    manual.mkdir(parents=True)
    (manual / "PM25_2024.csv").write_bytes(b"manual bytes")
    responses.get(FILE_URL, status=403)
    got = naps_engine.download("air/x/PM25_2024.csv")
    assert got["origin"] == "manual"
    assert got["content"] == b"manual bytes"


@responses.activate
def test_blocked_download_without_manual_copy_explains(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_DATA_DIR", tmp_path)
    responses.get(FILE_URL, status=403)
    with pytest.raises(http_client.HttpError, match="no manual copy"):
        naps_engine.download("air/x/PM25_2024.csv")


STATION_LIST = (
    '﻿"*Stations2024","",""\n'
    '"*Definitions_Définitions","",""\n'
    '"**********Stations2024**********","",""\n'
    '"NAPS_ID","Station_Name","Status","Latitude","Longitude","End_Date","NO2","O3","PM2.5_Continuous"\n'
    '"060410","TORONTO EAST","1","43.74792","-79.27406","","X","X","X"\n'
    '"010101","DUCKWORTH","0","47.56806","-52.70222","1988-07-26","","","H"\n'
    '"060999","OLD TORONTO","0","43.7","-79.4","2010-01-01","H","",""\n'
    '"060998","TORONTO SO2 ONLY","1","43.7","-79.4","","","",""\n'
    '"**********Definitions**********","",""\n'
    '"Status","1 = active","",""\n'
)


def test_station_list_parses_only_the_stations_section():
    stations = clean_engine.parse_naps_station_list(STATION_LIST.encode("utf-8"))
    assert [s["NAPS_ID"] for s in stations] == ["060410", "010101", "060999", "060998"]


def test_station_filter_uses_bbox_pollutants_and_end_date():
    stations = clean_engine.parse_naps_station_list(STATION_LIST.encode("utf-8"))
    keep = clean_engine.naps_stations_in_bbox(stations, config.REGION_BBOX, ["pm25", "o3", "no2"],
                                              open_since="2022-11-17")
    # outside the bbox, closed before the window, and measures none of ours are all dropped
    assert [s["NAPS_ID"] for s in keep] == ["060410"]


def test_station_list_without_section_raises():
    with pytest.raises(ValueError):
        clean_engine.parse_naps_station_list(b"a,b\n1,2\n")
