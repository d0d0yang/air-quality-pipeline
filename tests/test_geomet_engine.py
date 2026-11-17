"""
test_geomet_engine.py

Paging through `next` links and the filters each call sends.
"""

import re
from datetime import UTC, datetime

import responses

from pipeline import geomet_engine

ITEMS = re.compile(r"https://api\.weather\.gc\.ca/collections/climate-hourly/items.*")


def feature(i):
    return {"type": "Feature", "properties": {"UTC_DATE": f"2024-01-01T{i:02d}:00:00", "TEMP": i}}


@responses.activate
def test_follows_next_links_until_none():
    next_url = "https://api.weather.gc.ca/collections/climate-hourly/items?offset=2&limit=2"
    responses.get(ITEMS, json={"features": [feature(0), feature(1)],
                               "links": [{"rel": "next", "href": next_url}]})
    responses.get(ITEMS, json={"features": [feature(2)], "links": []})
    got = geomet_engine.climate_hourly("6158355", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert [f["properties"]["TEMP"] for f in got] == [0, 1, 2]
    assert "offset=2" in responses.calls[1].request.url


@responses.activate
def test_first_request_carries_filters():
    responses.get(ITEMS, json={"features": [], "links": []})
    geomet_engine.climate_hourly("6158355", datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC))
    url = responses.calls[0].request.url
    assert "CLIMATE_IDENTIFIER=6158355" in url
    assert "datetime=2024-01-01T00%3A00%3A00Z%2F2025-01-01T00%3A00%3A00Z" in url
    assert "limit=10000" in url


@responses.activate
def test_stops_on_empty_page_even_with_next_link():
    responses.get(ITEMS, json={"features": [], "links": [{"rel": "next", "href": "https://api.weather.gc.ca/x"}]})
    assert geomet_engine.climate_hourly("1", datetime(2024, 1, 1, tzinfo=UTC), None) == []
    assert len(responses.calls) == 1


def test_open_ended_range():
    assert geomet_engine.datetime_range(datetime(2026, 1, 1, tzinfo=UTC), None) == "2026-01-01T00:00:00Z/.."


@responses.activate
def test_climate_stations_drops_inactive():
    url = re.compile(r"https://api\.weather\.gc\.ca/collections/climate-stations/items.*")
    responses.get(url, json={"features": [
        {"properties": {"CLIMATE_IDENTIFIER": "a", "HLY_LAST_DATE": "2026-11-14 12:30:13"}},
        {"properties": {"CLIMATE_IDENTIFIER": "b", "HLY_LAST_DATE": "2011-03-01 00:00:00"}},
    ], "links": []})
    got = geomet_engine.climate_stations(active_since=datetime(2022, 11, 17, tzinfo=UTC))
    assert [f["properties"]["CLIMATE_IDENTIFIER"] for f in got] == ["a"]
