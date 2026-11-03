from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import fixture_text
from wxbrackets.climate_day import climate_day_window
from wxbrackets.db import DB
from wxbrackets.http import Http
from wxbrackets.rrfs import (
    DETERMINISTIC, ENSEMBLE, RRFS, GribPoint, byte_range, candidate_cycles, kelvin_to_f, needed_hours, parse_idx,
)

eccodes = pytest.importorskip("eccodes")
UTC = timezone.utc


def make_grib(value_k: float, short_name: str = "2t") -> bytes:
    """A tiny 3x3 lat/lon GRIB2 field around Chicago, every point = value_k."""
    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, val in [
            ("Ni", 3), ("Nj", 3),
            ("latitudeOfFirstGridPointInDegrees", 43.0), ("longitudeOfFirstGridPointInDegrees", 271.0),
            ("latitudeOfLastGridPointInDegrees", 41.0), ("longitudeOfLastGridPointInDegrees", 273.0),
            ("iDirectionIncrementInDegrees", 1.0), ("jDirectionIncrementInDegrees", 1.0),
        ]:
            eccodes.codes_set(gid, key, val)
        eccodes.codes_set_values(gid, [value_k] * 9)
        return eccodes.codes_get_message(gid)
    finally:
        eccodes.codes_release(gid)


# --- index files (real ones from noaa-rrfs-ops-pds) --------------------------------------


def test_parse_real_deterministic_index():
    entries = parse_idx(fixture_text("rrfs_2dfld_f025.idx"))
    assert entries[0][0] == 0 and entries[0][1].startswith("d=2026110306:REFC:")
    assert byte_range(entries, "TMAX:2 m above ground") == (82492487, entries[79][0] - 1)
    assert byte_range(entries, "TMP:2 m above ground") == (74982207, entries[74][0] - 1)
    assert byte_range(entries, "TMAX:10 m above ground") is None


def test_parse_real_ensemble_index_has_tmp_only():
    entries = parse_idx(fixture_text("refs_m001_f006.idx"))
    assert byte_range(entries, "TMP:2 m above ground")[0] == 12824907
    assert byte_range(entries, "TMAX:2 m above ground") is None


def test_last_message_reads_to_end_of_file():
    entries = parse_idx("1:0:d=1:A:surface:anl:\n2:500:d=1:B:surface:anl:\n")
    assert byte_range(entries, "B:surface") == (500, None)


# --- which hours and cycles ----------------------------------------------------------------


def test_needed_hours_for_interval_max_and_instant_values():
    start, end = climate_day_window(date(2026, 11, 4), ZoneInfo("America/Chicago"))
    init = datetime(2026, 11, 3, 6, tzinfo=UTC)  # day runs 06Z Nov 4 to 06Z Nov 5
    tmax = needed_hours(start, end, init, interval_max=True)
    tmp = needed_hours(start, end, init, interval_max=False)
    assert tmax == list(range(25, 49))  # hours ending 07Z ... 06Z: 24 one-hour maxima
    assert tmp == list(range(24, 49))  # 06Z ... 06Z inclusive: 25 readings


def test_candidate_cycles():
    start = datetime(2026, 11, 4, 6, tzinfo=UTC)
    end = start + timedelta(days=1)
    now = datetime(2026, 11, 3, 16, 27, tzinfo=UTC)
    # Newest first, every 6 hours; all reach 84 h past init.
    assert candidate_cycles(start, end, DETERMINISTIC, now)[:2] == [
        datetime(2026, 11, 3, 12, tzinfo=UTC), datetime(2026, 11, 3, 6, tzinfo=UTC)]
    # REFS reaches 60 h: a cycle older than end - 60 h can't cover the day.
    refs = candidate_cycles(start, end, ENSEMBLE, now)
    assert refs and all(c + timedelta(hours=60) >= end for c in refs)
    # A day too far out for any cycle.
    assert candidate_cycles(start + timedelta(days=5), end + timedelta(days=5), ENSEMBLE, now) == []
    # Same day: cycles must start no later than the day itself.
    same_day = candidate_cycles(start, end, DETERMINISTIC, start + timedelta(hours=10))
    assert same_day[0] == start


# --- decoding --------------------------------------------------------------------------------


def test_grib_point_reads_nearest_value():
    gp = GribPoint()
    assert gp.value(make_grib(290.0), 41.7841, -87.7551) == pytest.approx(290.0)
    assert gp.value(make_grib(280.5), 41.7841, -87.7551) == pytest.approx(280.5)  # cached index reused
    assert kelvin_to_f(273.15) == pytest.approx(32.0)


# --- full member max against a fake bucket ---------------------------------------------------


class FakeBucket:
    """Serves .idx and Range requests for files whose TMAX/TMP depend on the forecast hour."""

    def __init__(self, value_for_hour, missing_hours=()):
        self.value_for_hour = value_for_hour
        self.missing = set(missing_hours)
        self.ranges = []

    def file(self, fxx: int) -> tuple[bytes, str]:
        tmp = make_grib(self.value_for_hour(fxx) - 1)
        tmax = make_grib(self.value_for_hour(fxx))
        idx = (f"1:0:d=2026110306:TMP:2 m above ground:{fxx} hour fcst:\n"
               f"2:{len(tmp)}:d=2026110306:TMAX:2 m above ground:{fxx - 1}-{fxx} hour max fcst:\n")
        return tmp + tmax, idx

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fxx = int(path.split(".f")[1][:3])
        if fxx in self.missing:
            return httpx.Response(404)
        data, idx = self.file(fxx)
        if path.endswith(".idx"):
            return httpx.Response(200, text=idx)
        first, last = request.headers["Range"].removeprefix("bytes=").split("-")
        self.ranges.append(request.headers["Range"])
        return httpx.Response(206, content=data[int(first): int(last) + 1 if last else None])


def make_rrfs(bucket, tmp_path):
    http = Http("t", client=httpx.Client(transport=httpx.MockTransport(bucket)), sleep=lambda s: None)
    return RRFS(http, cache=DB(tmp_path / "c.db"), base_url="https://bucket.test")


def test_member_max_takes_the_highest_hourly_max(cfg, tmp_path):
    peak_hour = 33  # 15Z on Nov 4 from the 06Z Nov 3 run
    bucket = FakeBucket(lambda f: 290.0 if f == peak_hour else 280.0)
    rrfs = make_rrfs(bucket, tmp_path)
    now = datetime(2026, 11, 3, 11, tzinfo=UTC)  # 12Z run not out yet -> 06Z
    got = rrfs.member_max(DETERMINISTIC, cfg.station("CHI"), date(2026, 11, 4), 0, now)
    assert got == (pytest.approx(kelvin_to_f(290.0)), datetime(2026, 11, 3, 6, tzinfo=UTC))
    assert len(bucket.ranges) == 24  # one Range request per hour, TMAX message only
    # Second time comes from the cache: no new downloads.
    rrfs2 = make_rrfs(bucket, tmp_path)
    assert rrfs2.member_max(DETERMINISTIC, cfg.station("CHI"), date(2026, 11, 4), 0, now)[0] == got[0]
    assert len(bucket.ranges) == 24


def test_member_skipped_if_an_hour_is_missing(cfg, tmp_path):
    bucket = FakeBucket(lambda f: 280.0, missing_hours={30})
    rrfs = make_rrfs(bucket, tmp_path)
    now = datetime(2026, 11, 3, 11, tzinfo=UTC)
    assert rrfs.member_max(DETERMINISTIC, cfg.station("CHI"), date(2026, 11, 4), 0, now) is None


def test_falls_back_to_previous_cycle_until_the_newest_is_published(cfg, tmp_path):
    bucket = FakeBucket(lambda f: 285.0)
    newest_unpublished = {"t12z"}
    inner = bucket.__call__

    def handler(request):
        if any(tag in request.url.path for tag in newest_unpublished):
            return httpx.Response(404)
        return inner(request)

    http = Http("t", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    rrfs = RRFS(http, base_url="https://bucket.test")
    now = datetime(2026, 11, 3, 16, tzinfo=UTC)
    _, init = rrfs.member_max(DETERMINISTIC, cfg.station("CHI"), date(2026, 11, 4), 0, now)
    assert init == datetime(2026, 11, 3, 6, tzinfo=UTC)
