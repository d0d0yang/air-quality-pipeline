"""
openmeteo_engine.py

Weather forecasts from Open-Meteo for every air station's coordinates: the Historical Forecast API
for training and the Forecast API for tomorrow. Several points go in one request to keep the call count low.
"""

from pipeline import http_client

HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LIVE_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "boundary_layer_height",
    "precipitation",
    "cloud_cover",
]
MAX_POINTS_PER_CALL = 10
# ~10 km grid: fine for daily weather features, and it keeps a full backfill under the fair-use budget
GRID_DEG = 0.1


def snap(value):
    return round(round(value / GRID_DEG) * GRID_DEG, 4)


def point_id(lat, lon):
    return f"{snap(lat):.1f},{snap(lon):.1f}"


def _coords(points):
    lats = ",".join(f"{p['lat']:.4f}" for p in points)
    lons = ",".join(f"{p['lon']:.4f}" for p in points)
    return lats, lons


def _as_list(payload):
    # a single coordinate comes back as an object, several as a list
    return payload if isinstance(payload, list) else [payload]


def historical(points, start_date, end_date):
    """Archived forecasts between two dates (inclusive). Returns [(point, payload)] in input order."""
    if len(points) > MAX_POINTS_PER_CALL:
        raise ValueError(f"at most {MAX_POINTS_PER_CALL} points per call")
    lats, lons = _coords(points)
    params = {
        "latitude": lats,
        "longitude": lons,
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    payload = http_client.get(HISTORICAL_URL, params=params, timeout=180).json()
    return list(zip(points, _as_list(payload), strict=True))


def live(points, past_days=2, forecast_days=3):
    """Latest forecast run: a couple of past days (to close gaps) plus the next days."""
    if len(points) > MAX_POINTS_PER_CALL:
        raise ValueError(f"at most {MAX_POINTS_PER_CALL} points per call")
    lats, lons = _coords(points)
    params = {
        "latitude": lats,
        "longitude": lons,
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    payload = http_client.get(LIVE_URL, params=params, timeout=120).json()
    return list(zip(points, _as_list(payload), strict=True))
