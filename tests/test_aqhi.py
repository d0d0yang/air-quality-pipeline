"""
test_aqhi.py

The AQHI formula against hand-worked values, rounding, categories, colours and the 3-hour average.
"""

import math

import pytest

from pipeline import aqhi


def test_formula_known_values():
    # 10/10.4*100*((e^(0.000871*20)-1) + (e^(0.000537*30)-1) + (e^(0.000487*10)-1))
    assert aqhi.aqhi(20, 30, 10) == pytest.approx(3.720669, abs=1e-6)
    # a smoky hour: pm2.5 dominates
    assert aqhi.aqhi(5, 25, 150) == pytest.approx(9.006187, abs=1e-6)
    assert aqhi.aqhi(0, 0, 0) == 0


def test_missing_input_gives_none():
    assert aqhi.aqhi(None, 30, 10) is None
    assert aqhi.aqhi(20, math.nan, 10) is None


def test_rounding_floor_and_ten_plus():
    assert aqhi.rounded(0.2) == 1
    assert aqhi.rounded(3.49) == 3
    assert aqhi.rounded(3.5) == 4
    assert aqhi.rounded(10.4) == 10
    assert aqhi.rounded(10.6) == 11
    assert aqhi.label(10.6) == "10+"


def test_categories_match_eccc():
    assert [aqhi.category(v) for v in (1, 3, 4, 6, 7, 10, 11)] == [
        "low", "low", "moderate", "moderate", "high", "high", "very high"]


def test_colours_cover_the_scale():
    assert len(aqhi.COLOURS) == 11
    assert aqhi.colour(1) == "#00ccff"
    assert aqhi.colour(12) == "#660000"


def test_rolling_3h_needs_two_of_three():
    assert aqhi.rolling_3h([3, None, 6, 9]) == [None, None, 4.5, 7.5]
    assert aqhi.rolling_3h([1, 2, 3]) == [None, 1.5, 2.0]
