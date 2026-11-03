from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from conftest import fixture_json
from wxbrackets.climate_day import climate_date, climate_day_window
from wxbrackets.openmeteo import MemberSeries, member_daily_max, parse_members

CHI = ZoneInfo("America/Chicago")
UTC = timezone.utc


def test_window_is_standard_time_in_summer():
    # July: clocks are on CDT (UTC-5), but the climate day is midnight-midnight CST (UTC-6).
    start, end = climate_day_window(date(2026, 7, 15), CHI)
    assert start == datetime(2026, 7, 15, 6, tzinfo=UTC)
    assert end == datetime(2026, 7, 16, 6, tzinfo=UTC)
    assert start.astimezone(CHI).hour == 1  # 1:00 AM on the clock


def test_window_in_winter_and_on_transition_days():
    assert climate_day_window(date(2026, 1, 15), CHI)[0] == datetime(2026, 1, 15, 6, tzinfo=UTC)
    for day in (date(2026, 3, 8), date(2026, 11, 1)):  # DST starts / ends
        start, end = climate_day_window(day, CHI)
        assert start.hour == 6 and (end - start).total_seconds() == 86400


def test_after_midnight_dst_reading_belongs_to_previous_day():
    # 12:30 AM CDT on July 15 is 11:30 PM CST on July 14.
    assert climate_date(datetime(2026, 7, 15, 5, 30, tzinfo=UTC), CHI) == date(2026, 7, 14)
    assert climate_date(datetime(2026, 7, 15, 6, 30, tzinfo=UTC), CHI) == date(2026, 7, 15)


def test_member_max_uses_standard_time_window_in_dst():
    day = date(2026, 7, 15)
    t0 = int(datetime(2026, 7, 15, 0, tzinfo=UTC).timestamp())
    times = np.array([t0 + 3600 * h for h in range(36)])
    values = np.full(36, 80.0)
    values[5] = 99.0   # 05Z = 12:00 AM CDT Jul 15 = 11 PM CST Jul 14: previous climate day
    values[20] = 90.0  # 20Z = 3 PM CDT: inside
    series = MemberSeries("m", 0, times, values)
    start, end = climate_day_window(day, CHI)
    assert member_daily_max(series, start, end, min_valid=8) == 90.0
    # A naive local-clock midnight window would have picked up the 99.
    naive_start = datetime(2026, 7, 15, 0, tzinfo=CHI).astimezone(UTC)
    assert member_daily_max(series, naive_start, end, min_valid=8) == 99.0


def test_member_max_needs_enough_data():
    start, end = climate_day_window(date(2026, 11, 3), CHI)
    payload = fixture_json("openmeteo_combined.json")
    maxes = {(s.model, s.member): member_daily_max(s, start, end, 8)
             for s in parse_members(payload, ["ecmwf_ifs025", "gfs025"])}
    assert maxes[("ecmwf_ifs025", 2)] == 57.0  # the 70°F at 05Z belongs to Nov 2
    assert maxes[("gfs025", 2)] is None  # all nulls


def test_run_that_stops_before_the_day_ends_is_dropped():
    # HRRR's 18-hour runs often end mid-afternoon; their "max" would be too low.
    start, end = climate_day_window(date(2026, 11, 3), CHI)  # 06Z to 06Z
    t0 = int(start.timestamp())
    times = np.array([t0 + 3600 * h for h in range(25)])
    full = MemberSeries("m", 0, times, np.full(25, 50.0))
    assert member_daily_max(full, start, end, min_valid=8) == 50.0
    cut = MemberSeries("m", 0, times, np.where(np.arange(25) <= 14, 50.0, np.nan))  # ends 20Z
    assert member_daily_max(cut, start, end, min_valid=8) is None
    late = MemberSeries("m", 0, times, np.where(np.arange(25) >= 5, 50.0, np.nan))  # starts 11Z
    assert member_daily_max(late, start, end, min_valid=8) is None
    three_hourly = MemberSeries("m", 0, times, np.where(np.arange(25) % 3 == 0, 50.0, np.nan))
    assert member_daily_max(three_hourly, start, end, min_valid=8) == 50.0
