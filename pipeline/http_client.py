"""
http_client.py

One polite requests session for every source: our user agent, retries with backoff,
and Retry-After handling. Engines call get() instead of requests directly.
"""

import hashlib
import json
import time

import requests

from pipeline import config

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    pass


_session = None


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = config.USER_AGENT
    return _session


def _retry_after_seconds(resp, attempt):
    # honour the server's hint when there is one, otherwise exponential backoff
    hint = resp.headers.get("Retry-After") if resp is not None else None
    if hint and hint.strip().isdigit():
        return min(int(hint), 120)
    return min(2**attempt, 60)


def get(url, params=None, headers=None, retries=4, timeout=60, sleep=None, allow_status=()):
    """GET with retries. Returns the response, or raises HttpError after the last try."""
    sleep = sleep or time.sleep
    last_error = None
    for attempt in range(retries + 1):
        resp = None
        try:
            resp = session().get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code < 400 or resp.status_code in allow_status:
                return resp
            # drop the query string so signed urls never end up in logs
            last_error = f"HTTP {resp.status_code} for {resp.url.split('?')[0]}: {resp.text[:200]}"
            if resp.status_code not in RETRY_STATUS:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            sleep(_retry_after_seconds(resp, attempt))
    raise HttpError(last_error)


def get_json(url, params=None, headers=None, cache=False, before=None, after=None, **kwargs):
    """
    GET and parse json. With cache=True and DEV_CACHE=1 the response is kept in data/cache/http/,
    which callers only ask for when the data is in the past and won't change.
    `before()` / `after(resp)` only run for real network calls (that's where throttling belongs).
    """
    path = None
    if cache and config.DEV_CACHE:
        key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
        path = config.LOCAL_DATA_DIR / "cache" / "http" / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    if before:
        before()
    resp = get(url, params=params, headers=headers, **kwargs)
    if after:
        after(resp)
    data = resp.json()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    return data
