"""
geomet_engine.py

Pulls ECCC's MSC GeoMet OGC API collections: AQHI stations, observations and forecasts,
plus climate stations and their hourly weather. Pages by following the `next` links.
"""

from datetime import datetime

from pipeline import config, http_client

BASE = "https://api.weather.gc.ca/collections"
PAGE_LIMIT = 10000  # the server caps limit at 10000

CLIMATE_HOURLY_FIELDS = [
    "CLIMATE_IDENTIFIER", "UTC_DATE", "TEMP", "TEMP_FLAG", "RELATIVE_HUMIDITY", "RELATIVE_HUMIDITY_FLAG",
    "WIND_SPEED", "WIND_SPEED_FLAG", "WIND_DIRECTION", "WIND_DIRECTION_FLAG", "STATION_PRESSURE",
    "STATION_PRESSURE_FLAG", "PRECIP_AMOUNT", "PRECIP_AMOUNT_FLAG",
]


def bbox_param(bbox=None):
    return ",".join(str(x) for x in (bbox or config.REGION_BBOX))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def datetime_range(start=None, end=None):
    # ogc api uses ".." for an open end
    return f"{iso(start) if start else '..'}/{iso(end) if end else '..'}"


def iter_features(collection, params, max_pages=500):
    """Yields features page by page. The next link already carries the filters, so it's used as-is."""
    url = f"{BASE}/{collection}/items"
    query = {"f": "json", "limit": PAGE_LIMIT, **params}
    for _ in range(max_pages):
        page = http_client.get(url, params=query, timeout=120).json()
        yield from page.get("features", [])
        next_link = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not next_link or not page.get("features"):
            return
        url, query = next_link, None
    raise RuntimeError(f"{collection}: gave up after {max_pages} pages, check the filters")


def aqhi_stations(bbox=None):
    return list(iter_features("aqhi-stations", {"bbox": bbox_param(bbox)}))


def aqhi_observations(since=None, bbox=None):
    params = {"bbox": bbox_param(bbox), "sortby": "observation_datetime"}
    if since:
        params["datetime"] = datetime_range(since, None)
    return list(iter_features("aqhi-observations-realtime", params))


def aqhi_forecasts(since=None, bbox=None):
    params = {"bbox": bbox_param(bbox), "sortby": "publication_datetime"}
    if since:
        # the datetime filter applies to publication_datetime here (checked against the api)
        params["datetime"] = datetime_range(since, None)
    return list(iter_features("aqhi-forecasts-realtime", params))


def climate_stations(bbox=None, active_since: datetime | None = None):
    """Climate stations in the bbox with hourly data, optionally only those still reporting."""
    features = iter_features("climate-stations", {"bbox": bbox_param(bbox), "HAS_HOURLY_DATA": "Y"})
    keep = []
    for f in features:
        last = f["properties"].get("HLY_LAST_DATE") or ""
        if active_since and last[:10] < active_since.strftime("%Y-%m-%d"):
            continue
        keep.append(f)
    return keep


def climate_hourly(climate_id, start, end):
    """
    Hourly observations for one station. The datetime filter applies to LOCAL_DATE (checked
    against the api), so callers pad the window by a day and the merge dedupes on utc.
    """
    params = {
        "CLIMATE_IDENTIFIER": climate_id,
        "datetime": datetime_range(start, end),
        "properties": ",".join(CLIMATE_HOURLY_FIELDS),
        "sortby": "UTC_DATE",
    }
    return list(iter_features("climate-hourly", params))
