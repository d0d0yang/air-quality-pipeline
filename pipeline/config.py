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

# the runtime connects as the least-privilege etl_app user; only `db apply` uses the admin login
LOCAL_SQL_TEMPLATE = (
    "Driver={ODBC Driver 18 for SQL Server};Server=localhost,1433;Database=airqualitydb;"
    "Uid=etl_app;Pwd={LOCAL_ETL_PASSWORD};Encrypt=yes;TrustServerCertificate=yes"
)
LOCAL_ADMIN_SQL_TEMPLATE = (
    "Driver={ODBC Driver 18 for SQL Server};Server=localhost,1433;Database=airqualitydb;"
    "Uid=sa;Pwd={LOCAL_SA_PASSWORD};Encrypt=yes;TrustServerCertificate=yes"
)
# placeholders a connection string may contain; each is filled from the env var of the same name
PASSWORD_TOKENS = ("LOCAL_SA_PASSWORD", "LOCAL_ETL_PASSWORD", "AZ_SQL_ADMIN_PASSWORD", "AZ_SQL_ETL_PASSWORD")


def _get(name, default=None):
    # empty strings in .env count as unset
    value = os.environ.get(name, "")
    return value if value.strip() else default


def _bbox(raw):
    parts = [float(x) for x in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(f"REGION_BBOX needs 4 numbers (lon_min,lat_min,lon_max,lat_max), got {raw!r}")
    return tuple(parts)


def sql_conn_str(admin=False):
    # templates keep passwords out of the connection strings themselves
    if admin:
        template = _get("SQL_ADMIN_CONN_STR", LOCAL_ADMIN_SQL_TEMPLATE)
    else:
        template = _get("SQL_CONN_STR", LOCAL_SQL_TEMPLATE)
    for token in PASSWORD_TOKENS:
        template = template.replace("{" + token + "}", _get(token, ""))
    return template


def database_name():
    import re

    match = re.search(r"(?i)(?:Database|Initial Catalog)=([^;]+)", sql_conn_str())
    return match.group(1) if match else "airqualitydb"


ETL_PASSWORD = _get("LOCAL_ETL_PASSWORD")
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
# development only: reuse raw downloads of past data from data/cache/. benchmarks refuse to run with it on.
DEV_CACHE = _get("DEV_CACHE", "0") == "1"
USER_AGENT = _get("USER_AGENT", "air-quality-pipeline (github.com/d0d0yang/air-quality-pipeline)")
