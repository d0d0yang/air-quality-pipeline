"""
test_publish.py

The publish json schema check and the small formatting helpers.
"""

from datetime import date, datetime

import pytest

from pipeline import publish


def test_validate_accepts_complete_payloads():
    for name, keys in publish.SCHEMA.items():
        assert publish.validate(name, {k: None for k in keys})


def test_validate_names_missing_keys():
    with pytest.raises(ValueError, match="stations"):
        publish.validate("stations.json", {"generated_utc": "x", "hours_start_utc": "y"})


def test_num_rounds_and_drops_nan():
    assert publish.num(3.14159) == 3.14
    assert publish.num(float("nan")) is None
    assert publish.num(None) is None


def test_iso_formats():
    assert publish.iso(datetime(2026, 11, 17, 6, 0)) == "2026-11-17T06:00:00Z"
    assert publish.iso(date(2026, 11, 18)) == "2026-11-18"
    assert publish.iso(None) is None
