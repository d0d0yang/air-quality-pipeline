"""
config.py

Reads settings from .env (or real env vars) with sensible defaults.
Everything else imports from here instead of touching os.environ directly.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LOCAL_SQL_TEMPLATE = (
    "Driver={ODBC Driver 18 for SQL Server};Server=localhost,1433;Database=airqualitydb;"
    "Uid=sa;Pwd={LOCAL_SA_PASSWORD};Encrypt=yes;TrustServerCertificate=yes"
)


def _get(name, default=None):
    # empty strings in .env count as unset
    value = os.environ.get(name, "")
    return value if value.strip() else default


def _bbox(raw):
    parts = [float(x) for x in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(f"REGION_BBOX needs 4 numbers (lon_min,lat_min,lon_max,lat_max), got {raw!r}")
    return tuple(parts)


def sql_conn_str():
    # the local template keeps the password out of the connection string in .env
    template = _get("SQL_CONN_STR", LOCAL_SQL_TEMPLATE)
    return template.replace("{LOCAL_SA_PASSWORD}", _get("LOCAL_SA_PASSWORD", ""))


STORAGE_MODE = _get("STORAGE_MODE", "local")
LOCAL_DATA_DIR = (ROOT / _get("LOCAL_DATA_DIR", "./data")).resolve()
AZURE_STORAGE_CONNECTION_STRING = _get("AZURE_STORAGE_CONNECTION_STRING")
OPENAQ_API_KEY = _get("OPENAQ_API_KEY")
REGION_BBOX = _bbox(_get("REGION_BBOX", "-81.0,42.8,-78.5,44.6"))
POLLUTANTS = [p.strip() for p in _get("POLLUTANTS", "pm25,o3,no2").split(",") if p.strip()]
HISTORY_YEARS = int(_get("HISTORY_YEARS", "4"))
LOOKBACK_HOURS = int(_get("LOOKBACK_HOURS", "48"))
INCLUDE_LOW_COST_SENSORS = _get("INCLUDE_LOW_COST_SENSORS", "1") == "1"
MAX_MATCH_KM = float(_get("MAX_MATCH_KM", "25"))
OPENAQ_RPM = int(_get("OPENAQ_RPM", "50"))
REFRESH_SCHEDULE = _get("REFRESH_SCHEDULE", "daily")
USER_AGENT = _get("USER_AGENT", "air-quality-pipeline (github.com/d0d0yang/air-quality-pipeline)")
