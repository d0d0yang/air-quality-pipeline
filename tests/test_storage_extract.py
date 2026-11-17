"""
test_storage_extract.py

Local storage round trip, csv format, and one extract() call end to end with mocked http.
"""

import re

import responses

from pipeline import extract, storage


def test_write_csv_quotes_and_counts(tmp_path):
    store = storage.LocalStorage(tmp_path)
    n = storage.write_csv("landing/x/1/a.csv", ["a", "b"], iter([[1, None], ["x,y", 2]]), storage=store)
    assert n == 2
    text = (tmp_path / "landing/x/1/a.csv").read_text(encoding="utf-8")
    assert text == '"a","b"\n"1",""\n"x,y","2"\n'
    assert storage.read_csv("landing/x/1/a.csv", storage=store)[1] == {"a": "x,y", "b": "2"}
    assert store.list("landing/x") == ["landing/x/1/a.csv"]


def test_landing_path_is_safe():
    path = extract.landing_path("openaq_hours", 7, "openaq_hours:0001")
    assert path == "landing/openaq_hours/7/openaq_hours_0001.csv"


@responses.activate
def test_extract_aqhi_obs_writes_landing_and_watermark(tmp_path):
    url = re.compile(r"https://api\.weather\.gc\.ca/collections/aqhi-observations-realtime/items.*")
    responses.get(url, json={"features": [
        {"properties": {"location_id": "FEVNS", "observation_datetime": "2026-11-17T04:00:00Z", "aqhi": 2.47}},
        {"properties": {"location_id": "FEVNS", "observation_datetime": "2026-11-17T05:00:00Z", "aqhi": None}},
    ], "links": []})
    store = storage.LocalStorage(tmp_path)
    item = {"source": "aqhi_obs", "item_id": "aqhi_obs", "params": {"since": None}}
    result = extract.extract(42, item, store=store)
    assert result["rows_read"] == 2 and result["rows_written"] == 1
    assert result["files"] == ["landing/aqhi_obs/42/aqhi_obs.csv"]
    mark = result["new_watermark"]["source_state"][0]
    # the 05:00 hour had no value yet, so the watermark stops at 04:00
    assert mark["state_key"] == "max_utc" and mark["value_utc"] == "2026-11-17T04:00:00Z"
