"""
watermark.py

Works out what's new since the last run. State lives in etl.source_state and etl.sensor_state;
before those tables exist (or with no db at all) everything counts as never loaded.
"""

from datetime import UTC, datetime, timedelta

from pipeline import config


def now_hour():
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def window_start(now=None, years=None):
    # training window: HISTORY_YEARS back from today, at midnight utc
    now = now or now_hour()
    years = config.HISTORY_YEARS if years is None else years
    start = now - timedelta(days=round(365.25 * years))
    return start.replace(hour=0)


def incremental_from(last_hour, lookback_hours=None, floor=None):
    """Where an incremental pull starts: last loaded hour minus the lookback, never before floor."""
    lookback = config.LOOKBACK_HOURS if lookback_hours is None else lookback_hours
    if last_hour is None:
        return floor
    start = last_hour - timedelta(hours=lookback)
    return max(start, floor) if floor else start


def empty_state():
    return {"source": {}, "sensor": {}, "available": False, "note": ""}


def load_state():
    """Reads both state tables. Never raises: no db just means a cold start."""
    state = empty_state()
    try:
        from pipeline import db

        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT OBJECT_ID('etl.source_state'), OBJECT_ID('etl.sensor_state')")
            src_tbl, sensor_tbl = cur.fetchone()
            if src_tbl is None or sensor_tbl is None:
                state["note"] = "state tables not created yet (run `python -m pipeline.db apply`)"
                return state
            for source_id, state_key, value_text, value_time in cur.execute(
                "SELECT source_id, state_key, value_text, value_utc FROM etl.source_state"
            ).fetchall():
                state["source"][(source_id, state_key)] = _aware(value_time) or value_text
            for sensor_id, last_hour in cur.execute(
                "SELECT sensor_id, last_hour_utc FROM etl.sensor_state"
            ).fetchall():
                state["sensor"][int(sensor_id)] = _aware(last_hour)
        state["available"] = True
    except Exception as exc:  # noqa: BLE001 - any db problem means cold start for planning
        state["note"] = f"no state from db ({type(exc).__name__}), treating everything as new"
    return state


def _aware(value):
    # pyodbc hands back naive datetimes; everything in the db is utc
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
