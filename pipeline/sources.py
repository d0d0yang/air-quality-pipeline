"""
sources.py

The registry that drives the pipeline: every source, which engine pulls it, where it lands,
its natural key and how its watermark works. run.py, the function app and the sql merges all read this.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    id: str
    engine: str  # module in pipeline/ that plans and extracts it
    stg_table: str  # where the landing csv is bulk loaded
    merge_proc: str  # sql proc that moves stg -> core
    natural_key: tuple
    watermark: str  # how incremental mode decides what's new
    sequential: bool = False  # rate limited, so adf runs these items one at a time
    note: str = ""


SOURCES = [
    Source(
        "naps_stations", "naps_engine", "stg.naps_stations", "core.usp_merge_naps_stations",
        ("naps_id",), "file", note="naps station list (coordinates, site type, utc offset)",
    ),
    Source(
        "naps_hourly", "naps_engine", "stg.measurement", "core.usp_merge_measurement",
        ("station_id", "pollutant", "hour_utc", "source"), "file",
        note="validated hourly files, one per pollutant x year",
    ),
    Source(
        "aqhi_stations", "geomet_engine", "stg.aqhi_stations", "core.usp_merge_aqhi_stations",
        ("location_id",), "none",
    ),
    Source(
        "aqhi_obs", "geomet_engine", "stg.aqhi_obs", "core.usp_merge_aqhi_obs",
        ("location_id", "observation_utc"), "max_datetime",
        note="official hourly aqhi, the api keeps about 3 days",
    ),
    Source(
        "aqhi_forecast", "geomet_engine", "stg.aqhi_forecast", "core.usp_merge_aqhi_forecast",
        ("location_id", "publication_utc", "forecast_utc"), "max_datetime",
        note="official aqhi forecasts, about 3 days kept, so we archive them",
    ),
    Source(
        "climate_stations", "geomet_engine", "stg.climate_stations", "core.usp_merge_climate_stations",
        ("climate_id",), "none",
    ),
    Source(
        "climate_hourly", "geomet_engine", "stg.climate_hourly", "core.usp_merge_climate_hourly",
        ("climate_id", "hour_utc"), "station_hour", note="observed weather at eccc climate stations",
    ),
    Source(
        "openaq_locations", "openaq_engine", "stg.openaq_locations", "core.usp_merge_openaq_locations",
        ("sensor_id",), "none", sequential=True, note="locations, sensors, providers and licences",
    ),
    Source(
        "openaq_hours", "openaq_engine", "stg.measurement", "core.usp_merge_measurement",
        ("station_id", "pollutant", "hour_utc", "source"), "sensor_hour", sequential=True,
        note="hourly sensor values after the last naps year",
    ),
    Source(
        "openmeteo_hist", "openmeteo_engine", "stg.weather_forecast", "core.usp_merge_weather_forecast",
        ("point_id", "hour_utc"), "point_date", note="archived forecasts for training",
    ),
    Source(
        "openmeteo_live", "openmeteo_engine", "stg.weather_forecast", "core.usp_merge_weather_forecast",
        ("point_id", "hour_utc"), "none", note="tomorrow's forecast for prediction",
    ),
]

BY_ID = {s.id: s for s in SOURCES}

# canonical pollutant codes and units. naps and openaq names map onto these.
POLLUTANT_UNITS = {"pm25": "ug/m3", "pm10": "ug/m3", "o3": "ppb", "no2": "ppb"}
NAPS_FILE_CODES = {"pm25": "PM25", "pm10": "PM10", "o3": "O3", "no2": "NO2"}


def get(source_id):
    return BY_ID[source_id]


def sequential_ids():
    return [s.id for s in SOURCES if s.sequential]
