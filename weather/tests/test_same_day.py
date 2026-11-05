"""Same-day forecasts: what has already happened plus what the models say about the hours left."""

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from wxbrackets import paper
from wxbrackets.brackets import make_brackets
from wxbrackets.climate_day import climate_day_window
from wxbrackets.config import PaperSettings
from wxbrackets.forecast import build_forecast
from wxbrackets.openmeteo import MemberSeries, member_daily_max, remaining_max

CHI = ZoneInfo("America/Chicago")
UTC = timezone.utc
DAY = date(2026, 11, 5)
START, END = climate_day_window(DAY, CHI)  # 06Z to 06Z


def series(peak_hour_utc: int, peak: float, base: float = 50.0) -> MemberSeries:
    t0 = int(START.timestamp())
    times = np.array([t0 + 3600 * h for h in range(25)])
    values = np.full(25, base)
    values[(peak_hour_utc - 6) % 24] = peak
    return MemberSeries("m", 0, times, values)


def test_remaining_max_ignores_hours_already_past():
    s = series(peak_hour_utc=20, peak=66.0)  # 2 PM CST peak
    assert member_daily_max(s, START, END, 8) == 66.0
    morning = START + timedelta(hours=9)  # 9 AM CST: the peak is still ahead
    assert member_daily_max(s, START, END, 8, since=morning) == 66.0
    evening = START + timedelta(hours=18)  # 6 PM CST: it has passed
    assert member_daily_max(s, START, END, 8, since=evening) == 50.0
    assert remaining_max(s, END + timedelta(minutes=1), END) == -math.inf  # day over


def test_remaining_max_needs_the_run_to_reach_the_end_of_the_day():
    s = series(20, 66.0)
    s.values[-6:] = np.nan  # run stops 5 hours early
    assert remaining_max(s, START + timedelta(hours=18), END) is None


def forecast(cfg, maxes, observed, remaining=True):
    brackets = make_brackets([59, 61, 63, 65, 67])
    return build_forecast(cfg, cfg.station("CHI"), DAY, maxes, brackets, START + timedelta(hours=18),
                          observed_max_f=observed, remaining=remaining)


def test_after_the_peak_the_forecast_collapses_onto_the_observed_high(cfg):
    # Observed 61.0 by 6 PM; every member says the rest of the evening stays in the 50s.
    maxes = {("ecmwf_ifs025", i): 55.0 + i * 0.5 for i in range(6)}
    f = forecast(cfg, maxes, observed=61.0)
    probs = dict(zip([b.label for b in f.brackets], f.probs))
    assert probs["60-61"] > 0.75  # 61 (or a degree more, from peaks between observations)
    assert probs["≤59"] == 0 and probs["64-65"] < 0.01 and probs["66-67"] < 1e-6 and probs["≥68"] < 1e-6
    assert all(v == 61.0 for _, _, v, _ in f.members)  # stored as each member's final high


def test_old_behaviour_would_have_kept_a_warm_afternoon_alive(cfg):
    """The bug: whole-day maxima from members with a warm afternoon kept 66-67 alive at 6 PM."""
    maxes = {("ecmwf_ifs025", i): 66.0 for i in range(6)}
    # Whole-day maxima, only floored at the observation (the old way): most mass at 66-67.
    old = forecast(cfg, maxes, observed=61.0, remaining=False)
    assert dict(zip([b.label for b in old.brackets], old.probs))["66-67"] > 0.5
    # Remaining hours: the afternoon is over, nothing warm is left.
    later = {k: 54.0 for k in maxes}
    new = forecast(cfg, later, observed=61.0)
    assert dict(zip([b.label for b in new.brackets], new.probs))["66-67"] < 1e-6


def test_members_still_warming_keep_their_upside(cfg):
    # 10 AM: observed 55 so far, members expect 64-66 this afternoon.
    maxes = {("ecmwf_ifs025", i): 64.0 + i * 0.4 for i in range(6)}
    f = forecast(cfg, maxes, observed=55.0)
    probs = dict(zip([b.label for b in f.brackets], f.probs))
    assert probs["64-65"] + probs["66-67"] > 0.8


def test_day_over_for_every_member(cfg):
    maxes = {("ecmwf_ifs025", i): -math.inf for i in range(6)}
    f = forecast(cfg, maxes, observed=62.4)
    probs = dict(zip([b.label for b in f.brackets], f.probs))
    assert probs["62-63"] > 0.6 and sum(f.probs) == pytest.approx(1.0)


SETTINGS = PaperSettings()  # cutoffs: highs 12:00, lows 06:00


def test_same_day_cutoff():
    today = DAY
    morning = START + timedelta(hours=9, minutes=30)
    afternoon = START + timedelta(hours=12)
    assert paper.same_day_block(DAY + timedelta(days=1), today, START, afternoon, None, SETTINGS) is None  # tomorrow
    assert paper.same_day_block(DAY, today, START, morning, 55.0, SETTINGS) is None
    assert "12:00" in paper.same_day_block(DAY, today, START, afternoon, 55.0, SETTINGS)
    # Lows stop at 6 AM: by dawn the low is mostly in.
    assert paper.same_day_block(DAY, today, START, START + timedelta(hours=5), 40.0, SETTINGS, "low") is None
    assert "6:00" in paper.same_day_block(DAY, today, START, START + timedelta(hours=7), 40.0, SETTINGS, "low")
    assert "no observations" in paper.same_day_block(DAY, today, START, morning, None, SETTINGS)
