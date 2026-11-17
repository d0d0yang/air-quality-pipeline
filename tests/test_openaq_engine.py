"""
test_openaq_engine.py

The throttle (per-minute spacing, hourly cap, header backoff), paging, and 429 handling, all without real time.
"""

import re
from datetime import UTC, datetime

import pytest
import responses

from pipeline import config, openaq_engine

HOURS = re.compile(r"https://api\.openaq\.org/v3/sensors/\d+/hours.*")
LOCATIONS = re.compile(r"https://api\.openaq\.org/v3/locations.*")


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make_throttle(rpm=60, cap=1900):
    clock = FakeClock()
    return openaq_engine.Throttle(rpm, hourly_cap=cap, clock=clock.now, sleep=clock.sleep), clock


def test_throttle_spaces_requests_by_rpm():
    t, clock = make_throttle(rpm=30)
    for _ in range(3):
        t.wait()
    assert clock.slept == [2.0, 2.0]


def test_throttle_does_not_wait_when_already_slow():
    t, clock = make_throttle(rpm=60)
    t.wait()
    clock.t += 5
    t.wait()
    assert clock.slept == []


def test_throttle_respects_hourly_cap():
    t, clock = make_throttle(rpm=6000, cap=3)
    for _ in range(4):
        t.wait()
    # the 4th call waits until the first one is an hour old
    assert clock.t >= 3600


def test_throttle_backs_off_when_headers_say_empty():
    t, clock = make_throttle()
    t.from_headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "21"})
    assert clock.slept == [22.0]
    t.from_headers({"x-ratelimit-remaining": "40", "x-ratelimit-reset": "21"})
    assert clock.slept == [22.0]


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(config, "OPENAQ_API_KEY", "test-key")
    monkeypatch.setattr(openaq_engine, "_throttle", openaq_engine.Throttle(6000, sleep=lambda s: None))


def hour(i):
    return {"value": i, "period": {"datetimeFrom": {"utc": f"2025-01-01T{i % 24:02d}:00:00Z"}}}


@responses.activate
def test_pages_until_short_page(api_key, monkeypatch):
    monkeypatch.setattr(openaq_engine, "PAGE_LIMIT", 2)
    responses.get(HOURS, json={"results": [hour(0), hour(1)]})
    responses.get(HOURS, json={"results": [hour(2)]})
    rows, finished = openaq_engine.sensor_hours(617, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC))
    assert finished and [r["value"] for r in rows] == [0, 1, 2]
    assert "page=2" in responses.calls[1].request.url
    assert responses.calls[0].request.headers["X-API-Key"] == "test-key"


@responses.activate
def test_deadline_stops_paging(api_key, monkeypatch):
    monkeypatch.setattr(openaq_engine, "PAGE_LIMIT", 1)
    responses.get(HOURS, json={"results": [hour(0)]})
    rows, finished = openaq_engine.sensor_hours(1, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC),
                                                deadline=0)
    assert rows == [] and finished is False


@responses.activate
def test_429_is_retried(api_key, no_sleep):
    responses.get(LOCATIONS, status=429, headers={"Retry-After": "3"})
    responses.get(LOCATIONS, json={"results": [{"id": 1}]})
    assert openaq_engine.locations() == [{"id": 1}]
    assert 3 in no_sleep


def test_missing_key_is_explained(monkeypatch):
    monkeypatch.setattr(config, "OPENAQ_API_KEY", None)
    with pytest.raises(RuntimeError, match="explore.openaq.org"):
        openaq_engine.locations()


def test_estimate_requests():
    assert openaq_engine.estimate_requests(0) == 1
    assert openaq_engine.estimate_requests(1000) == 1
    assert openaq_engine.estimate_requests(1001) == 2
