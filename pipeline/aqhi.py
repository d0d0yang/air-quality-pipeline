"""
aqhi.py

Canada's Air Quality Health Index from NO2, O3 and PM2.5, plus the official risk categories and colours.
Used for stations that have pollutant data but no official AQHI, and checked against ECCC's own values.
"""

import math

# formula: AQHI = 10/10.4 * 100 * [(e^(0.000871*NO2) - 1) + (e^(0.000537*O3) - 1) + (e^(0.000487*PM2.5) - 1)]
# with 3-hour trailing averages, NO2 and O3 in ppb, PM2.5 in ug/m3.
# source: Stieb et al. (2008), "A new multipollutant, no-threshold air quality health index based on
# short-term associations observed in daily time-series analyses", J. Air & Waste Manage. Assoc. 58:435-450,
# as presented by ECCC (C. Audette, cec.org/wp-content/uploads/2020/02/celine_audette.pdf).
# checked with scripts/check_aqhi.py: eccc's observation_datetime marks the END of the last averaged hour,
# so the official value at time t matches our hour_utc = t - 1 h (mean abs difference 0.006 over 1,309 station-hours).
COEF_NO2 = 0.000871
COEF_O3 = 0.000537
COEF_PM25 = 0.000487
SCALE = 10 / 10.4 * 100

# categories from canada.ca/en/environment-climate-change/services/air-quality-health-index/about.html
CATEGORIES = [(1, 3, "low"), (4, 6, "moderate"), (7, 10, "high"), (11, math.inf, "very high")]

# official colours for 1..10 and 10+, sampled from ECCC's scale image
# weather.gc.ca/cacheable/images/img/aqhi_scale_small_en.png
COLOURS = ["#00ccff", "#0099cc", "#006699", "#ffff00", "#ffcc00", "#ff9933",
           "#ff6666", "#ff0000", "#cc0000", "#990000", "#660000"]


def _missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def aqhi(no2_ppb, o3_ppb, pm25_ugm3):
    """Unrounded AQHI from 3-hour averages. Returns None if any input is missing."""
    if any(_missing(v) for v in (no2_ppb, o3_ppb, pm25_ugm3)):
        return None
    total = (
        (math.exp(COEF_NO2 * no2_ppb) - 1)
        + (math.exp(COEF_O3 * o3_ppb) - 1)
        + (math.exp(COEF_PM25 * pm25_ugm3) - 1)
    )
    return SCALE * total


def rounded(value):
    """The published index: rounded to a whole number, at least 1, anything over 10 shown as 11 ("10+")."""
    if _missing(value):
        return None
    # float() also accepts the Decimals the database hands back
    return min(max(int(math.floor(float(value) + 0.5)), 1), 11)


def category(value):
    r = rounded(value)
    if r is None:
        return None
    return next(name for lo, hi, name in CATEGORIES if lo <= r <= hi)


def colour(value):
    r = rounded(value)
    return COLOURS[r - 1] if r is not None else None


def label(value):
    r = rounded(value)
    if r is None:
        return None
    return "10+" if r == 11 else str(r)


def rolling_3h(values):
    """
    Trailing 3-hour means over an hourly list (oldest first). Needs at least 2 of the 3 hours,
    otherwise None, so one missing hour doesn't blank the index.
    """
    out = []
    for i in range(len(values)):
        window = [v for v in values[max(0, i - 2): i + 1] if not _missing(v)]
        out.append(sum(window) / len(window) if len(window) >= 2 else None)
    return out
