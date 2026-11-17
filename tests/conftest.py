"""
conftest.py

Shared test setup: no real sleeping, and a fresh http session per test so `responses` sees every call.
"""

import pytest

from pipeline import http_client, openaq_engine


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(http_client.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(http_client, "_session", None)
    monkeypatch.setattr(openaq_engine, "_throttle", None)
    return slept
