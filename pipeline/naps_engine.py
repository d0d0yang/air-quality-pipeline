"""
naps_engine.py

Finds and downloads the NAPS continuous hourly files and the station list from ECCC's data portal.
Cleaning (the wide-to-long reshape, sentinels, time zones) happens later in clean_engine.py.
"""

import hashlib
import urllib.parse
from pathlib import Path

from pipeline import config, http_client
from pipeline.sources import NAPS_FILE_CODES

API = "https://data-donnees.az.ec.gc.ca/api"
ROOT = "air/monitor/national-air-pollution-surveillance-naps-program"
STATION_LIST = f"{ROOT}/ProgramInformation-InformationProgramme/StationsNAPS-StationsSNPA.csv"
PORTAL_PAGE = "https://data-donnees.az.ec.gc.ca/data/" + ROOT


def hourly_dir(year):
    return f"{ROOT}/Data-Donnees/{year}/ContinuousData-DonneesContinu/HourlyData-DonneesHoraires"


def list_dir(path):
    """Directory listing from the portal api. Paths go in without a leading slash."""
    resp = http_client.get(f"{API}/path_contents", params={"path": path}, allow_status=(404,))
    if resp.status_code == 404:
        return []
    return resp.json().get("path_contents", [])


def discover_hourly_files(years, pollutants):
    """One entry per pollutant x year that actually exists on the portal."""
    wanted = {NAPS_FILE_CODES[p]: p for p in pollutants if p in NAPS_FILE_CODES}
    found = []
    for year in years:
        for entry in list_dir(hourly_dir(year)):
            code = entry["name"].rsplit("_", 1)[0]
            if entry["is_directory"] or code not in wanted or not entry["name"].endswith(f"_{year}.csv"):
                continue
            found.append({
                "path": entry["path"],
                "name": entry["name"],
                "year": year,
                "pollutant": wanted[code],
                "last_modified": entry["last_modified"],
            })
    return found


def latest_hourly_year(start_year, end_year):
    """Newest year that has continuous hourly data (the portal lags a year or more)."""
    for year in range(end_year, start_year - 1, -1):
        if any(e["name"].startswith("PM25_") for e in list_dir(hourly_dir(year))):
            return year
    return None


def station_list_info():
    parent, name = STATION_LIST.rsplit("/", 1)
    for entry in list_dir(parent):
        if entry["name"] == name:
            return {"path": entry["path"], "name": name, "last_modified": entry["last_modified"]}
    return {"path": STATION_LIST, "name": name, "last_modified": None}


def manual_path(name):
    return config.LOCAL_DATA_DIR / "manual" / "naps" / name


def download(path, etag=None):
    """
    Downloads one portal file. Returns {content, etag, last_modified, sha256, origin}, with
    content None when the server says it hasn't changed. Falls back to data/manual/naps/ if blocked.
    """
    url = f"{API}/file?path=" + urllib.parse.quote("/" + path.lstrip("/"), safe="")
    headers = {"If-None-Match": etag} if etag else None
    name = path.rsplit("/", 1)[-1]
    try:
        resp = http_client.get(url, headers=headers, timeout=180, allow_status=(304,))
    except http_client.HttpError as exc:
        local = manual_path(name)
        if local.exists():
            return _from_bytes(local.read_bytes(), origin="manual")
        raise http_client.HttpError(
            f"naps download failed for {name} and no manual copy in {local.parent} ({exc})"
        ) from exc
    if resp.status_code == 304:
        return {"content": None, "etag": etag, "last_modified": None, "sha256": None, "origin": "portal"}
    result = _from_bytes(resp.content, origin="portal")
    result["etag"] = resp.headers.get("ETag")
    result["last_modified"] = resp.headers.get("Last-Modified")
    return result


def _from_bytes(content, origin):
    return {
        "content": content,
        "etag": None,
        "last_modified": None,
        "sha256": hashlib.sha256(content).hexdigest(),
        "origin": origin,
    }


def manual_files():
    folder = config.LOCAL_DATA_DIR / "manual" / "naps"
    return sorted(Path(folder).glob("*.csv")) if folder.exists() else []
