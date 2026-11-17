"""
test_db.py

Database tests against the local SQL Server container (`docker compose up -d`, then `pytest -m db`).
They build a separate airqualitydb_test database, so the real one is never touched.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import db

pytestmark = pytest.mark.db
TEST_DB = "airqualitydb_test"


@pytest.fixture(scope="module")
def conn():
    db.apply(database=TEST_DB, verbose=False)
    with db.connect(admin=True, database=TEST_DB) as c:
        yield c


@pytest.fixture(autouse=True)
def clean(conn):
    cur = conn.cursor()
    for table in ("core.measurement", "core.station_xref", "core.station_weather_match", "core.weather_point",
                  "core.climate_station", "core.openaq_sensor", "etl.dq_result", "etl.stage_run", "etl.sensor_state",
                  "etl.source_state", "mart.daily_station", "stg.measurement", "stg.naps_stations"):
        cur.execute(f"DELETE FROM {table}")
    cur.execute("DELETE FROM core.station")
    cur.execute("DELETE FROM etl.pipeline_run")
    yield


def start_run(conn, mode="incremental"):
    return db.call(conn, "etl.usp_start_run", mode=mode, target="local", trigger_name="pytest")[0]["run_id"]


def end_run(conn, run_id, status="succeeded"):
    db.call(conn, "etl.usp_end_run", run_id=run_id, status=status)


def add_station(conn, key, source, lat, lon, name, low_cost=0, monitor=1):
    conn.cursor().execute(
        "INSERT INTO core.station (station_key, source, source_ref, name, lat, lon, is_monitor, is_low_cost_sensor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", key, source, key.split(":", 1)[1], name, lat, lon, monitor, low_cost)
    return conn.cursor().execute("SELECT station_id FROM core.station WHERE station_key = ?", key).fetchone()[0]


def stage_measurements(conn, item_id, rows):
    cur = conn.cursor()
    for source, ref, pollutant, hour, value in rows:
        cur.execute("INSERT INTO stg.measurement (item_id, source, station_ref, sensor_id, pollutant, hour_utc, value, "
                    "unit, method_code, coverage_pct, qc_flag) VALUES (?, ?, ?, '', ?, ?, ?, 'ug/m3', '', '', '')",
                    item_id, source, ref, pollutant, hour, value)


def merge(conn, run_id, item_id):
    return db.call(conn, "core.usp_merge_measurement", run_id=run_id, item_id=item_id)[0]


ROWS = [("naps", "060410", "pm25", "2024-07-01T05:00:00Z", "3"),
        ("naps", "060410", "pm25", "2024-07-01T06:00:00Z", "4"),
        ("naps", "060410", "pm25", "2024-07-01T07:00:00Z", "5")]


def test_sql_applies_twice(conn):
    # idempotent ddl: a second apply changes nothing and doesn't fail
    db.apply(database=TEST_DB, verbose=False)
    tables = conn.cursor().execute("SELECT COUNT(*) FROM sys.tables WHERE SCHEMA_NAME(schema_id) IN "
                                   "('stg','core','mart','etl')").fetchone()[0]
    assert tables >= 20


def test_merge_twice_is_a_no_op(conn):
    add_station(conn, "naps:060410", "naps", 43.74792, -79.27406, "TORONTO EAST")
    run_id = start_run(conn)
    stage_measurements(conn, "a", ROWS)
    first = merge(conn, run_id, "a")
    stage_measurements(conn, "a", ROWS)
    second = merge(conn, run_id, "a")
    assert (first["rows_inserted"], first["rows_updated"]) == (3, 0)
    assert (second["rows_inserted"], second["rows_updated"]) == (0, 0)


def test_late_correction_is_one_update(conn):
    add_station(conn, "naps:060410", "naps", 43.74792, -79.27406, "TORONTO EAST")
    run_id = start_run(conn)
    stage_measurements(conn, "a", ROWS)
    merge(conn, run_id, "a")
    corrected = [ROWS[0], ROWS[1], ("naps", "060410", "pm25", "2024-07-01T07:00:00Z", "9")]
    stage_measurements(conn, "a", corrected)
    result = merge(conn, run_id, "a")
    assert (result["rows_inserted"], result["rows_updated"]) == (0, 1)
    value = conn.cursor().execute(
        "SELECT value FROM core.measurement WHERE hour_utc = '2024-07-01T07:00:00'").fetchone()[0]
    assert float(value) == 9.0
    assert conn.cursor().execute("SELECT COUNT(*) FROM core.measurement").fetchone()[0] == 3


def test_unknown_station_and_bad_hour_are_rejected_not_loaded(conn):
    add_station(conn, "naps:060410", "naps", 43.74792, -79.27406, "TORONTO EAST")
    run_id = start_run(conn)
    stage_measurements(conn, "a", [ROWS[0], ("naps", "999999", "pm25", "2024-07-01T05:00:00Z", "1"),
                                   ("naps", "060410", "pm25", "not a time", "1")])
    result = merge(conn, run_id, "a")
    assert result["rows_inserted"] == 1 and result["rows_rejected"] == 2
    logged = conn.cursor().execute("SELECT COUNT(*) FROM etl.dq_result WHERE check_name = 'type_conversion'")
    assert logged.fetchone()[0] == 1


def test_duplicate_across_sources_is_one_row_in_the_analysis_view(conn):
    add_station(conn, "naps:060410", "naps", 43.74792, -79.27406, "TORONTO EAST")
    add_station(conn, "openaq:1275803", "openaq", 43.74792, -79.27406, "Toronto East")
    run_id = start_run(conn)
    db.call(conn, "core.usp_link_stations", run_id=run_id)
    stage_measurements(conn, "a", [("naps", "060410", "pm25", "2024-07-01T05:00:00Z", "3"),
                                   ("openaq", "1275803", "pm25", "2024-07-01T05:00:00Z", "4")])
    merge(conn, run_id, "a")
    cur = conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM core.measurement").fetchone()[0] == 2
    rows = cur.execute("SELECT source, value FROM core.v_measurement_best").fetchall()
    assert len(rows) == 1 and rows[0][0] == "naps"  # naps wins on precedence


def test_station_linking_rules(conn):
    naps = add_station(conn, "naps:060434", "naps", 43.5856, -79.6572, "MISSISSAUGA - 3359 MISSISSAUGA RD")
    near_typo = add_station(conn, "openaq:1365268", "openaq", 43.5856, -79.6572, "Misssissauga")
    low_cost = add_station(conn, "openaq:9", "openaq", 43.5857, -79.6572, "Mississauga", low_cost=1, monitor=0)
    far = add_station(conn, "openaq:10", "openaq", 43.5856, -79.68, "Mississauga")   # ~1.9 km west
    aqhi = add_station(conn, "aqhi:FEAKO", "aqhi", 43.62, -79.62, "Mississauga")     # ~5 km away
    run_id = start_run(conn)
    db.call(conn, "core.usp_link_stations", run_id=run_id)
    site = dict(conn.cursor().execute("SELECT station_id, site_id FROM core.station_xref").fetchall())
    assert site[near_typo] == naps        # 0 m and the same name despite the typo
    assert site[low_cost] == low_cost     # low-cost sensors never merge with a monitor
    assert site[far] == far               # same name but over 250 m
    assert site[aqhi] == naps             # aqhi location joins by name within 10 km


def test_nearest_weather_station_on_known_coordinates(conn):
    naps = add_station(conn, "naps:060410", "naps", 43.74792, -79.27406, "TORONTO EAST")
    cur = conn.cursor()
    # toronto city (about 11 km away) and toronto intl airport (about 21 km), real coordinates
    cur.execute("INSERT INTO core.climate_station (climate_id, name, lat, lon) VALUES "
                "('6158355', 'TORONTO CITY', 43.666667, -79.4), ('6158731', 'TORONTO INTL A', 43.676667, -79.630556)")
    cur.execute("INSERT INTO core.weather_point (point_id, lat, lon) VALUES ('43.7,-79.3', 43.7, -79.3)")
    run_id = start_run(conn)
    db.call(conn, "core.usp_link_stations", run_id=run_id)
    db.call(conn, "core.usp_match_stations", run_id=run_id, max_match_km=10)
    climate_id, km, used, point = cur.execute(
        "SELECT climate_id, climate_distance_km, is_used, point_id FROM core.station_weather_match "
        "WHERE station_id = ?", naps).fetchone()
    assert climate_id == "6158355"
    assert 12 < float(km) < 14
    assert used is False or used == 0      # over the 10 km limit given here, so flagged and not used
    assert point == "43.7,-79.3"


def test_dq_catches_a_future_timestamp(conn):
    add_station(conn, "naps:060410", "naps", 43.74792, -79.27406, "TORONTO EAST")
    run_id = start_run(conn)
    db.call(conn, "core.usp_link_stations", run_id=run_id)
    future = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:00:00Z")
    stage_measurements(conn, "a", [("naps", "060410", "pm25", future, "5")])
    merge(conn, run_id, "a")
    with pytest.raises(Exception, match="no_future_measurements"):
        db.call(conn, "etl.usp_run_dq_checks", run_id=run_id)
    status = conn.cursor().execute("SELECT status FROM etl.dq_result WHERE run_id = ? AND check_name = "
                                   "'no_future_measurements'", run_id).fetchone()[0]
    assert status == "fail"


def test_sensor_watermark_only_moves_over_contiguous_data(conn):
    run_id = start_run(conn)

    def apply(segs):
        db.call(conn, "etl.usp_apply_watermarks", run_id=run_id, json=json.dumps({"sensor_state": segs}))
        row = conn.cursor().execute("SELECT last_hour_utc FROM etl.sensor_state WHERE sensor_id = 7").fetchone()
        return row[0].strftime("%Y-%m-%dT%H") if row else None

    seg = {"sensor_id": 7, "finished": True, "first_segment": False, "last_hour_utc": None}
    # a later segment can't start a watermark: an earlier one may have failed
    assert apply([{**seg, "from": "2025-02-10T05:00:00Z", "to": "2025-03-22T05:00:00Z"}]) is None
    first = {**seg, "first_segment": True, "from": "2025-01-01T05:00:00Z", "to": "2025-02-10T05:00:00Z"}
    assert apply([first]) == "2025-02-10T04"
    # gap: starts after the watermark + 1 h, ignored
    assert apply([{**seg, "from": "2025-03-22T05:00:00Z", "to": "2025-04-01T05:00:00Z"}]) == "2025-02-10T04"
    # contiguous: moves on
    assert apply([{**seg, "from": "2025-02-10T05:00:00Z", "to": "2025-03-22T05:00:00Z"}]) == "2025-03-22T04"


def test_full_mode_start_clears_facts_but_keeps_stations(conn):
    add_station(conn, "naps:060410", "naps", 43.74792, -79.27406, "TORONTO EAST")
    run_id = start_run(conn)
    stage_measurements(conn, "a", ROWS)
    merge(conn, run_id, "a")
    end_run(conn, run_id)
    start_run(conn, mode="full")
    cur = conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM core.measurement").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM core.station").fetchone()[0] == 1


def test_second_run_cannot_start_while_one_is_running(conn):
    start_run(conn)
    with pytest.raises(Exception, match="still running"):
        start_run(conn)
