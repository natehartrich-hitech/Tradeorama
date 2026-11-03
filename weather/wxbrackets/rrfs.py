"""NOAA RRFS (deterministic) and REFS (its 5-member ensemble) from the operational AWS bucket.

Only the 2 m temperature message for the station is fetched from each file: the .idx
index gives its byte range, an HTTP Range request downloads that one message (~1.7 MB),
and eccodes reads the value at the grid point nearest the station.

Layout (checked against noaa-rrfs-ops-pds in November 2026):
  rrfs.YYYYMMDD/HH/rrfs.tHHz.2dfld.3km.fNNN.conus.grib2                  hourly cycles; 84 h at 00/06/12/18Z
  rrfsens.YYYYMMDD/HH/mMMM/rrfs.tHHz.mMMM.2dfldnomads.3km.fNNN.conus.grib2  00/06/12/18Z; 60 h; m001-m005
The deterministic files carry TMAX (max over the preceding hour), which catches peaks
between hours; the ensemble files only carry instantaneous TMP.
"""

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .climate_day import climate_day_window
from .config import Model, Station
from .http import Http

log = logging.getLogger(__name__)

BASE_URL = "https://noaa-rrfs-ops-pds.s3.amazonaws.com"


@dataclass(frozen=True)
class Product:
    template: str  # path under the bucket; {ymd} {hh} {fxx:03d} {mem:03d}
    field: str  # idx descriptor, e.g. "TMAX:2 m above ground"
    interval_max: bool  # True: value at fxx is the max over (fxx-1h, fxx]
    cycle_every_h: int
    max_fxx: int


DETERMINISTIC = Product(
    "rrfs.{ymd}/{hh}/rrfs.t{hh}z.2dfld.3km.f{fxx:03d}.conus.grib2", "TMAX:2 m above ground", True, 6, 84
)
ENSEMBLE = Product(
    "rrfsens.{ymd}/{hh}/m{mem:03d}/rrfs.t{hh}z.m{mem:03d}.2dfldnomads.3km.f{fxx:03d}.conus.grib2",
    "TMP:2 m above ground", False, 6, 60,
)


def parse_idx(text: str) -> list[tuple[int, str]]:
    """(byte offset, descriptor) for each message in a wgrib2-style .idx file.

    A line looks like "79:82492487:d=2026110306:TMAX:2 m above ground:24-25 hour max fcst:"
    and its descriptor is everything from "d=" on.
    """
    out = []
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            out.append((int(parts[1]), parts[2]))
    return out


def byte_range(entries: list[tuple[int, str]], field: str) -> tuple[int, int | None] | None:
    """Start and inclusive end byte of the first message whose descriptor contains `field`.
    End is None for the last message in the file (read to the end)."""
    var, _, level = field.partition(":")
    for i, (offset, desc) in enumerate(entries):
        # desc looks like "d=2026110212:TMAX:2 m above ground:5-6 hour max fcst:"
        bits = desc.split(":")
        if len(bits) >= 3 and bits[1] == var and bits[2] == level:
            end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
            return offset, end
    return None


def needed_hours(start: datetime, end: datetime, init: datetime, interval_max: bool) -> list[int]:
    """Forecast hours whose values cover the climate day [start, end].

    Interval maxima are stamped at the END of their hour, so the day needs the hours ending
    after `start` up to `end`; instantaneous values need every hour from `start` to `end`.
    """
    first = start + timedelta(hours=1) if interval_max else start
    hours = []
    t = first
    while t <= end:
        hours.append(int((t - init).total_seconds() // 3600))
        t += timedelta(hours=1)
    return hours


def candidate_cycles(start: datetime, end: datetime, product: Product, now: datetime, lookback: int = 4) -> list[datetime]:
    """Newest first: cycles initialised no later than the day's start (so they cover all of it)
    and long enough to reach its end."""
    latest = min(start, now).replace(minute=0, second=0, microsecond=0)
    latest -= timedelta(hours=latest.hour % product.cycle_every_h)
    out = []
    t = latest
    # Older cycles reach less far ahead, so stop at the first one that can't reach the end.
    while len(out) < lookback and t + timedelta(hours=product.max_fxx) >= end:
        out.append(t)
        t -= timedelta(hours=product.cycle_every_h)
    return out


def kelvin_to_f(k: float) -> float:
    return (k - 273.15) * 9 / 5 + 32


class GribPoint:
    """Reads the value nearest a lat/lon from GRIB2 messages, finding the grid index once per grid."""

    def __init__(self):
        import eccodes  # imported here so the rest of the package works without it

        self.ec = eccodes
        self._index: dict[tuple, int] = {}

    def value(self, message: bytes, lat: float, lon: float) -> float:
        ec = self.ec
        gid = ec.codes_new_from_message(message)
        try:
            grid = tuple(ec.codes_get(gid, k) for k in (
                "gridType", "Nx", "Ny", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
            )) + (round(lat, 4), round(lon, 4))
            if grid not in self._index:
                self._index[grid] = ec.codes_grib_find_nearest(gid, lat, lon)[0].index
            v = ec.codes_get_double_element(gid, "values", self._index[grid])
            missing = ec.codes_get_double(gid, "missingValue") if ec.codes_is_defined(gid, "missingValue") else None
            if missing is not None and v == missing:
                return math.nan
            return v
        finally:
            ec.codes_release(gid)


class RRFS:
    def __init__(self, http: Http, cache=None, base_url: str = BASE_URL, grib: GribPoint | None = None):
        self.http = http
        self.cache = cache  # object with get_point(url, field, lat, lon) / put_point(...)
        self.base_url = base_url.rstrip("/")
        self._grib = grib
        self._idx: dict[str, list[tuple[int, str]] | None] = {}

    @property
    def grib(self) -> GribPoint:
        if self._grib is None:
            self._grib = GribPoint()
        return self._grib

    def url(self, product: Product, init: datetime, fxx: int, member: int = 0) -> str:
        path = product.template.format(ymd=init.strftime("%Y%m%d"), hh=init.strftime("%H"), fxx=fxx, mem=member)
        return f"{self.base_url}/{path}"

    def index(self, url: str) -> list[tuple[int, str]] | None:
        if url not in self._idx:
            try:
                self._idx[url] = parse_idx(self.http.get(url + ".idx").text)
            except Exception as exc:  # 404 = not published (yet)
                log.debug("no index for %s: %s", url, exc)
                self._idx[url] = None
        return self._idx[url]

    def point(self, url: str, field: str, lat: float, lon: float) -> float | None:
        if self.cache is not None:
            hit = self.cache.get_point(url, field, lat, lon)
            if hit is not None:
                return hit
        entries = self.index(url)
        rng = byte_range(entries, field) if entries else None
        if rng is None:
            return None
        start, end = rng
        r = self.http.get(url, headers={"Range": f"bytes={start}-{'' if end is None else end}"})
        value_f = kelvin_to_f(self.grib.value(r.content, lat, lon))
        if math.isnan(value_f):
            return None
        if self.cache is not None:
            self.cache.put_point(url, field, lat, lon, value_f)
        return value_f

    def choose_cycle(self, product: Product, start: datetime, end: datetime, now: datetime, member: int) -> datetime | None:
        """Newest cycle whose last needed file has been published."""
        for init in candidate_cycles(start, end, product, now):
            last = needed_hours(start, end, init, product.interval_max)[-1]
            if self.index(self.url(product, init, last, member)) is not None:
                return init
        return None

    def member_max(self, product: Product, station: Station, day: date, member: int, now: datetime) -> tuple[float, datetime] | None:
        """(daily max °F, cycle used), or None if no cycle covers the whole climate day."""
        start, end = climate_day_window(day, station.tz)
        init = self.choose_cycle(product, start, end, now, member)
        if init is None:
            return None
        values = []
        for fxx in needed_hours(start, end, init, product.interval_max):
            v = self.point(self.url(product, init, fxx, member), product.field, station.lat, station.lon)
            if v is None:
                log.warning("RRFS f%03d missing for %s member %d; skipping this member", fxx, init, member)
                return None
            values.append(v)
        return max(values), init

    def check(self, product: Product, station: Station, day: date, member: int, now: datetime | None = None) -> str:
        """Describe availability without downloading GRIB data (for check-models)."""
        start, end = climate_day_window(day, station.tz)
        init = self.choose_cycle(product, start, end, now or datetime.now(timezone.utc), member)
        if init is None:
            return "no published cycle covers this day"
        first = needed_hours(start, end, init, product.interval_max)[0]
        entries = self.index(self.url(product, init, first, member)) or []
        found = byte_range(entries, product.field) is not None
        return f"cycle {init:%Y-%m-%d %HZ}, field {product.field!r} {'found' if found else 'MISSING'}"

    def member_maxes(self, station: Station, day: date, models: list[Model], now: datetime | None = None) -> dict[tuple[str, int], float]:
        now = now or datetime.now(timezone.utc)
        out = {}
        for m in models:
            if m.source == "rrfs":
                got = self.member_max(DETERMINISTIC, station, day, 0, now)
                if got:
                    out[(m.id, 0)] = got[0]
            elif m.source == "refs":
                for member in m.members or range(1, 6):
                    got = self.member_max(ENSEMBLE, station, day, member, now)
                    if got:
                        out[(m.id, member)] = got[0]
        return out
