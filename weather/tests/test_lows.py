"""Daily lows: the same pipeline as highs, mirrored."""

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest

from conftest import fixture_json
from wxbrackets.brackets import make_brackets
from wxbrackets.climate_day import climate_day_window
from wxbrackets.distribution import HighDistribution
from wxbrackets.forecast import build_forecast
from wxbrackets.nws import parse_point_forecast_low, parse_running_max
from wxbrackets.openmeteo import MemberSeries, member_daily_max, remaining_max
from wxbrackets.polymarket_us import parse_event
from wxbrackets.rrfs import DETERMINISTIC, kelvin_to_f

CHI = ZoneInfo("America/Chicago")
UTC = timezone.utc
DAY = date(2026, 11, 6)
START, END = climate_day_window(DAY, CHI)


def series(values: dict[int, float], base: float = 45.0) -> MemberSeries:
    t0 = int(START.timestamp())
    times = np.array([t0 + 3600 * h for h in range(25)])
    v = np.full(25, base)
    for hour, value in values.items():
        v[hour] = value
    return MemberSeries("m", 0, times, v)


def test_member_low_and_remaining_low():
    s = series({6: 36.0, 23: 39.0})  # 6 AM CST dawn low, then a cool evening
    assert member_daily_max(s, START, END, 8, kind="low") == 36.0
    after_dawn = START + timedelta(hours=9)
    assert member_daily_max(s, START, END, 8, since=after_dawn, kind="low") == 39.0  # dawn has passed
    assert remaining_max(s, END + timedelta(minutes=1), END, kind="low") == math.inf


def test_ceiling_truncation():
    d = HighDistribution(np.array([40.0]), np.array([1.0]), bandwidth=2.0).conditioned_on_ceiling(41.5)
    probs = d.bracket_probs(make_brackets([39, 41, 43]))
    assert probs[2] == 0 and probs[3] == 0 and sum(probs) == pytest.approx(1.0)
    pinned = HighDistribution(np.array([50.0]), np.array([1.0]), bandwidth=0.5).conditioned_on_ceiling(38.5)
    assert pinned.bracket_probs(make_brackets([38, 40])) == [1.0, 0.0, 0.0]


def forecast(cfg, maxes, observed=None, remaining=False):
    brackets = make_brackets([36, 38, 40, 42, 44])
    return build_forecast(cfg, cfg.station("CHI"), DAY, maxes, brackets, START + timedelta(hours=10),
                          observed_max_f=observed, remaining=remaining, kind="low")


def test_after_dawn_the_low_collapses_onto_the_observed_minimum(cfg):
    # 10 AM: observed low 38.0; every member's rest-of-day minimum is milder (evening in the 40s).
    maxes = {("ecmwf_ifs025", i): 42.0 + i for i in range(6)}
    f = forecast(cfg, maxes, observed=38.0, remaining=True)
    probs = dict(zip([b.label for b in f.brackets], f.probs))
    assert probs["37-38"] > 0.75 and probs["≥45"] < 1e-6 and probs["41-42"] < 0.01
    assert all(v == 38.0 for _, _, v, _ in f.members)


def test_a_cold_evening_can_still_undercut_the_morning_low(cfg):
    maxes = {("ecmwf_ifs025", i): 33.0 + 0.3 * i for i in range(6)}  # cold front tonight
    f = forecast(cfg, maxes, observed=38.0, remaining=True)
    probs = dict(zip([b.label for b in f.brackets], f.probs))
    assert probs["≤36"] > 0.8


def test_low_forecast_uses_its_own_distribution_settings(cfg):
    import dataclasses
    from wxbrackets.config import DistributionSettings

    low_cfg = dataclasses.replace(cfg, distribution_low=DistributionSettings(bias_f=-2.0, spread_inflation=1.0))
    maxes = {("ecmwf_ifs025", i): 40.0 for i in range(4)}
    assert forecast(low_cfg, maxes).dist.mean() == pytest.approx(38.0, abs=0.01)
    assert forecast(cfg, maxes).dist.mean() == pytest.approx(40.0, abs=0.01)  # highs' settings untouched


def test_running_min_and_nws_overnight_low(cfg):
    st = cfg.station("CHI")
    start, end = climate_day_window(date(2026, 11, 3), st.tz)
    rm = parse_running_max(fixture_json("nws_observations.json"), start, end)
    assert rm.value("low") == pytest.approx(12.2 * 9 / 5 + 32)  # 54.0°F at 15:53Z; 05:53Z is the previous day
    fc = fixture_json("nws_forecast.json")
    assert parse_point_forecast_low(fc, date(2026, 11, 3), st) == 42  # "Tonight" from Nov 2 evening
    assert parse_point_forecast_low(fc, date(2026, 11, 4), st) == 45


def test_real_low_event_parses():
    ms = parse_event(fixture_json("pmus_temp_mdwlow_2026-11-06.json")["event"])
    assert [m.bracket.label for m in ms] == ["≤36", "37-38", "39-40", "41-42", "43-44", "≥45"]
    assert ms[0].slug == "tc-temp-mdwlow-2026-11-06-lt37f"


def test_rrfs_low_uses_hourly_minimum(cfg, tmp_path):
    from test_rrfs import FakeBucket, make_rrfs

    bucket = FakeBucket(lambda f: 270.0 if f == 31 else 280.0)  # coldest hour ends 13Z (7 AM CST)
    rrfs = make_rrfs(bucket, tmp_path)
    now = datetime(2026, 11, 3, 11, tzinfo=UTC)
    low, _ = rrfs.member_max(DETERMINISTIC, cfg.station("CHI"), date(2026, 11, 4), 0, now, kind="low")
    assert low == pytest.approx(kelvin_to_f(270.0 - 3))  # TMIN = value - 3 in the fake files
    high, _ = rrfs.member_max(DETERMINISTIC, cfg.station("CHI"), date(2026, 11, 4), 0, now)
    assert high == pytest.approx(kelvin_to_f(280.0))
