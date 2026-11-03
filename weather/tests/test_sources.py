from datetime import date, datetime, timezone

import httpx
import pytest

from conftest import fixture_json
from wxbrackets.climate_day import climate_day_window
from wxbrackets.http import Http
from wxbrackets.nws import parse_point_forecast_high, parse_running_max


def test_running_max_uses_climate_day_and_skips_rejected(cfg):
    st = cfg.station("CHI")
    start, end = climate_day_window(date(2026, 11, 3), st.tz)
    rm = parse_running_max(fixture_json("nws_observations.json"), start, end)
    # 68°F at 05:53Z is the previous climate day; 86°F was rejected by QC ("X"); nulls skipped.
    assert rm.max_f == pytest.approx(60.08)
    assert rm.at == datetime(2026, 11, 3, 18, 53, tzinfo=timezone.utc)
    assert rm.observations == 3


def test_point_forecast_high(cfg):
    st = cfg.station("CHI")
    fc = fixture_json("nws_forecast.json")
    assert parse_point_forecast_high(fc, date(2026, 11, 3), st) == 60
    assert parse_point_forecast_high(fc, date(2026, 11, 4), st) == 55
    assert parse_point_forecast_high(fc, date(2026, 11, 9), st) is None


def test_http_retries_with_backoff_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    sleeps = []
    http = Http("t", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, backoff=1.0)
    assert http.json("https://api.weather.gov/x") == {"ok": True}
    assert len(calls) == 3
    assert 1.0 <= sleeps[0] <= 1.25 and 2.0 <= sleeps[1] <= 2.5  # exponential with jitter


def test_http_honours_retry_after_and_gives_up():
    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "7"})))
    http = Http("t", client=client, sleep=sleeps.append, retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        http.get("https://api.weather.gov/x")
    assert sleeps == [7.0, 7.0]


def test_http_spaces_requests_per_host():
    now = [100.0]
    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    http = Http("t", min_interval={"api.weather.gov": 1.0}, client=client,
                sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
    http.get("https://api.weather.gov/a")
    http.get("https://api.weather.gov/b")
    http.get("https://other.example/c")
    assert sleeps == [1.0]
