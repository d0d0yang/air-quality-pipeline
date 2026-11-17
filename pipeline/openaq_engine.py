"""
openaq_engine.py

OpenAQ v3 client: locations in the bbox (with sensors, provider and licence) and hourly values per sensor.
Throttled to OPENAQ_RPM and the hourly cap, and it backs off when the rate-limit headers say so.
"""

import time
from collections import deque

from pipeline import config, http_client

BASE = "https://api.openaq.org/v3"
PAGE_LIMIT = 1000
HOURLY_CAP = 2000  # free key: 60/min and 2000/hour
HOURLY_MARGIN = 100


class Throttle:
    """Token-bucket style spacing plus a rolling one-hour window, driven by an injectable clock."""

    def __init__(self, rpm, hourly_cap=HOURLY_CAP - HOURLY_MARGIN, clock=time.monotonic, sleep=time.sleep):
        self.interval = 60.0 / max(rpm, 1)
        self.hourly_cap = hourly_cap
        self.clock = clock
        self.sleep = sleep
        self.last = None
        self.window = deque()
        self.waited = 0.0

    def wait(self):
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            self._pause(self.interval - (now - self.last))
        while self.window and self.clock() - self.window[0] >= 3600:
            self.window.popleft()
        if len(self.window) >= self.hourly_cap:
            self._pause(3600 - (self.clock() - self.window[0]) + 1)
        self.last = self.clock()
        self.window.append(self.last)

    def from_headers(self, headers):
        # the server knows best: if it says we're out, wait for its reset
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is not None and reset is not None and str(remaining).isdigit() and int(remaining) <= 1:
            self._pause(min(float(reset), 3600) + 1)

    def _pause(self, seconds):
        if seconds > 0:
            self.waited += seconds
            self.sleep(seconds)


_throttle = None


def throttle():
    global _throttle
    if _throttle is None:
        _throttle = Throttle(config.OPENAQ_RPM)
    return _throttle


def _get(path, params=None):
    if not config.OPENAQ_API_KEY:
        raise RuntimeError("OPENAQ_API_KEY is not set (get a free key at explore.openaq.org)")
    t = throttle()
    t.wait()
    resp = http_client.get(f"{BASE}{path}", params=params, headers={"X-API-Key": config.OPENAQ_API_KEY})
    t.from_headers(resp.headers)
    return resp.json()


def _paged(path, params, deadline=None):
    """Follows page numbers until a short page. Returns (results, finished)."""
    results, page = [], 1
    while True:
        if deadline is not None and time.monotonic() > deadline:
            return results, False
        body = _get(path, {**params, "limit": PAGE_LIMIT, "page": page})
        batch = body.get("results", [])
        results.extend(batch)
        if len(batch) < PAGE_LIMIT:
            return results, True
        page += 1


def locations(bbox=None):
    bbox = bbox or config.REGION_BBOX
    results, _ = _paged("/locations", {"bbox": ",".join(str(x) for x in bbox)})
    return results


def sensor_hours(sensor_id, datetime_from, datetime_to, deadline=None):
    """Hourly aggregates for one sensor. `finished` is False if the deadline cut it short."""
    params = {
        "datetime_from": datetime_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datetime_to": datetime_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return _paged(f"/sensors/{sensor_id}/hours", params, deadline=deadline)


def estimate_requests(hours):
    # one page per 1000 hours, at least one request even for an empty window
    return max(1, -(-int(hours) // PAGE_LIMIT))
