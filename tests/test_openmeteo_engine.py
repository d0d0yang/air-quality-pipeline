"""
test_openmeteo_engine.py

Multi-location requests map back to the right points, and the grid snapping.
"""

import re
from datetime import date

import pytest
import responses

from pipeline import openmeteo_engine as om

HIST = re.compile(r"https://historical-forecast-api\.open-meteo\.com/v1/forecast.*")
LIVE = re.compile(r"https://api\.open-meteo\.com/v1/forecast.*")


def payload(lat, temp):
    return {"latitude": lat, "hourly": {"time": ["2024-01-01T00:00"], "temperature_2m": [temp]}}


@responses.activate
def test_multi_location_keeps_input_order():
    points = [{"point_id": "43.7,-79.4", "lat": 43.7, "lon": -79.4},
              {"point_id": "43.2,-79.8", "lat": 43.2, "lon": -79.8}]
    responses.get(HIST, json=[payload(43.7, 1.0), payload(43.2, 2.0)])
    got = om.historical(points, date(2024, 1, 1), date(2024, 1, 1))
    assert [(p["point_id"], body["hourly"]["temperature_2m"][0]) for p, body in got] == [
        ("43.7,-79.4", 1.0), ("43.2,-79.8", 2.0)]
    url = responses.calls[0].request.url
    assert "latitude=43.7000%2C43.2000" in url
    assert "timezone=UTC" in url


@responses.activate
def test_single_location_object_is_wrapped():
    responses.get(LIVE, json=payload(43.7, 3.0))
    got = om.live([{"point_id": "43.7,-79.4", "lat": 43.7, "lon": -79.4}])
    assert len(got) == 1 and got[0][1]["hourly"]["temperature_2m"] == [3.0]


@responses.activate
def test_count_mismatch_raises():
    responses.get(LIVE, json=[payload(1, 1), payload(2, 2)])
    with pytest.raises(ValueError):
        om.live([{"point_id": "x", "lat": 1, "lon": 1}])


def test_too_many_points_rejected():
    points = [{"point_id": str(i), "lat": 43, "lon": -79} for i in range(om.MAX_POINTS_PER_CALL + 1)]
    with pytest.raises(ValueError):
        om.historical(points, date(2024, 1, 1), date(2024, 1, 2))


def test_point_id_snaps_to_grid():
    assert om.point_id(43.6758, -79.3969) == "43.7,-79.4"
    assert om.point_id(43.6758, -79.3969) == om.point_id(43.7041, -79.4412)
