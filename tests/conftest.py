"""
conftest.py

Shared test setup: no real sleeping, and a fresh http session per test so `responses` sees every call.
"""

import pytest

from pipeline import config, http_client, openaq_engine


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # tests never read or write the dev download cache
    monkeypatch.setattr(config, "DEV_CACHE", False)
    slept = []
    monkeypatch.setattr(http_client.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(http_client, "_session", None)
    monkeypatch.setattr(openaq_engine, "_throttle", None)
    return slept
