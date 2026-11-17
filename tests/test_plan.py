"""
test_plan.py

Work-item planning without the network: openaq segmenting and packing, naps change detection,
incremental windows, and the open-meteo fair-use weighting.
"""

from datetime import UTC, datetime, timedelta

from pipeline import config, plan
from pipeline import watermark as wm

NOW = datetime(2026, 11, 17, 6, tzinfo=UTC)
START = wm.window_start(NOW, years=4)
HANDOVER = datetime(2025, 1, 1, 5, tzinfo=UTC)


def location(loc_id, monitor=True, params=("pm25", "o3"), first="2016-01-01T00:00:00Z", last="2026-11-17T05:00:00Z"):
    return {
        "id": loc_id, "isMonitor": monitor,
        "coordinates": {"latitude": 43.7, "longitude": -79.4},
        "datetimeFirst": {"utc": first}, "datetimeLast": {"utc": last},
        "sensors": [{"id": loc_id * 10 + i, "parameter": {"name": p}} for i, p in enumerate(params + ("pm1",))],
    }


def ctx_with(locations):
    return {"openaq_locations": locations, "naps_stations": [], "aqhi_stations": [], "climate_stations": []}


def test_window_start_is_four_years_back_at_midnight():
    assert START == datetime(2022, 11, 17, tzinfo=UTC)


def test_incremental_from_applies_lookback_and_floor():
    last = datetime(2026, 11, 16, 12, tzinfo=UTC)
    assert wm.incremental_from(last, 48) == last - timedelta(hours=48)
    assert wm.incremental_from(last, 48, floor=last) == last
    assert wm.incremental_from(None, 48, floor=START) == START


def test_openaq_full_starts_at_handover_and_skips_other_params():
    items = plan.plan_openaq("full", ctx_with([location(1)]), wm.empty_state(), START, HANDOVER, NOW)
    segments = [s for i in items if i["source"] == "openaq_hours" for s in i["params"]["segments"]]
    assert {s["pollutant"] for s in segments} == {"pm25", "o3"}  # pm1 ignored
    assert min(s["from"] for s in segments) == "2025-01-01T05:00:00Z"
    assert items[0]["source"] == "openaq_locations"


def test_openaq_items_stay_under_request_budget():
    locs = [location(i) for i in range(1, 30)]
    items = plan.plan_openaq("full", ctx_with(locs), wm.empty_state(), START, HANDOVER, NOW)
    budget = plan.OPENAQ_ITEM_SECONDS * config.OPENAQ_RPM // 60
    assert all(i["est_requests"] <= budget for i in items)


def test_openaq_incremental_uses_sensor_watermark():
    state = wm.empty_state()
    state["sensor"] = {10: NOW - timedelta(hours=3), 11: NOW - timedelta(hours=3)}
    items = plan.plan_openaq("incremental", ctx_with([location(1)]), state, START, HANDOVER, NOW)
    segments = [s for i in items if i["source"] == "openaq_hours" for s in i["params"]["segments"]]
    expected = wm.incremental_from(NOW - timedelta(hours=3), config.LOOKBACK_HOURS)
    assert {s["from"] for s in segments} == {plan._iso(expected)}
    assert sum(i["est_requests"] for i in items if i["source"] == "openaq_hours") == 2


def test_openaq_dead_sensor_is_skipped():
    dead = location(1, last="2019-09-05T00:00:00Z")
    items = plan.plan_openaq("full", ctx_with([dead]), wm.empty_state(), START, HANDOVER, NOW)
    assert [i for i in items if i["source"] == "openaq_hours"] == []


def test_openaq_low_cost_excluded_when_switched_off(monkeypatch):
    monkeypatch.setattr(config, "INCLUDE_LOW_COST_SENSORS", False)
    assert plan.openaq_sensors([location(1, monitor=False)], START, HANDOVER, NOW) == []


def naps_ctx():
    return {
        "station_list": {"path": "x/StationsNAPS-StationsSNPA.csv", "last_modified": "2026-09-29"},
        "naps_files": [{"path": "p/PM25_2024.csv", "pollutant": "pm25", "year": 2024, "last_modified": "2026-06-24"},
                       {"path": "p/O3_2024.csv", "pollutant": "o3", "year": 2024, "last_modified": "2026-06-24"}],
    }


def test_naps_incremental_skips_unchanged_files():
    state = wm.empty_state()
    state["source"] = {("naps_hourly", "p/PM25_2024.csv"): "2026-06-24",
                       ("naps_stations", "x/StationsNAPS-StationsSNPA.csv"): "2026-09-29"}
    items = plan.plan_naps("incremental", naps_ctx(), state)
    assert [i["item_id"] for i in items] == ["naps_hourly:o3:2024"]


def test_naps_full_takes_everything():
    items = plan.plan_naps("full", naps_ctx(), wm.empty_state())
    assert len(items) == 3


def test_openaq_minutes_include_the_hourly_cap():
    assert plan.openaq_minutes(1500, rpm=50) == 30.0
    # 1900 requests use up the first hour, then 100 more at 50/min
    assert plan.openaq_minutes(2000, rpm=50) == 62.0


def test_openmeteo_full_is_split_by_year_and_weighted():
    ctx = ctx_with([location(1)])
    items, points = plan.plan_openmeteo("full", ctx, wm.empty_state(), START, NOW)
    hist = [i for i in items if i["source"] == "openmeteo_hist"]
    assert len(points) == 1
    assert [i["params"]["start_date"][:4] for i in hist] == ["2022", "2023", "2024", "2025", "2026"]
    # one point, a whole year, 7 variables: 27 two-week blocks
    assert hist[1]["est_requests"] == 27


def test_openmeteo_incremental_only_pulls_recent_days():
    ctx = ctx_with([location(1)])
    state = wm.empty_state()
    state["source"] = {("openmeteo_hist", "43.7,-79.4"): "2026-11-16"}
    items, _ = plan.plan_openmeteo("incremental", ctx, state, START, NOW)
    hist = [i for i in items if i["source"] == "openmeteo_hist"]
    assert len(hist) == 1 and hist[0]["params"]["start_date"] == "2026-11-15"
