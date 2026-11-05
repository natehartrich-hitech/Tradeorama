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
import re
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
    interval_max: bool  # True: file fxx covers (fxx-1h, fxx]
    cycle_every_h: int
    max_fxx: int
    multi: bool = False  # several time steps of `field` per file (15-minute output)
    low_field: str | None = None  # field for daily LOWS (default: `field`, for instantaneous products)

    def field_for(self, kind: str) -> str:
        return self.field if kind == "high" else (self.low_field or self.field)


DETERMINISTIC = Product(
    "rrfs.{ymd}/{hh}/rrfs.t{hh}z.2dfld.3km.f{fxx:03d}.conus.grib2", "TMAX:2 m above ground", True, 6, 84,
    low_field="TMIN:2 m above ground",
)
# Hourly RRFS runs on AWS only publish 15-minute output for 18 hours: file fNNN holds TMP at
# (N-1)h15m, (N-1)h30m, (N-1)h45m and Nh. Used for the rest of TODAY, where freshness matters.
SUBHOURLY = Product(
    "rrfs.{ymd}/{hh}/rrfs.t{hh}z.2dfld.3km.subh.f{fxx:03d}.conus.grib2", "TMP:2 m above ground", True, 1, 18, multi=True
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


def byte_ranges(entries: list[tuple[int, str]], field: str) -> list[tuple[int, int | None, str]]:
    """Every message matching `field`: (start, inclusive end or None, descriptor)."""
    var, _, level = field.partition(":")
    out = []
    for i, (offset, desc) in enumerate(entries):
        bits = desc.split(":")
        if len(bits) >= 3 and bits[1] == var and bits[2] == level:
            out.append((offset, entries[i + 1][0] - 1 if i + 1 < len(entries) else None, desc))
    return out


def lead_minutes(desc: str) -> int | None:
    """Forecast time in minutes from a descriptor like "d=2026110413:TMP:2 m above ground:255 min fcst:"."""
    bits = desc.split(":")
    when = bits[3] if len(bits) > 3 else ""
    if m := re.fullmatch(r"(\d+) min fcst", when):
        return int(m.group(1))
    if m := re.fullmatch(r"(\d+) hour fcst", when):
        return 60 * int(m.group(1))
    return None


def recent_hourly_runs(since: datetime, now: datetime, lookback_h: int = 6) -> list[datetime]:
    """Newest first: hourly runs started by `since` whose 18-hour output still reaches past it."""
    t = min(since, now).replace(minute=0, second=0, microsecond=0)
    out = []
    for _ in range(lookback_h + 1):
        if t + timedelta(hours=SUBHOURLY.max_fxx) > since:
            out.append(t)
        t -= timedelta(hours=1)
    return out


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
        return self.values(message, [(lat, lon)])[0]

    def values(self, message: bytes, points: list[tuple[float, float]]) -> list[float]:
        """Values nearest each (lat, lon), decoding the message once. NaN where the field is missing."""
        ec = self.ec
        gid = ec.codes_new_from_message(message)
        try:
            grid = tuple(ec.codes_get(gid, k) for k in (
                "gridType", "Nx", "Ny", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
            ))
            missing = ec.codes_get_double(gid, "missingValue") if ec.codes_is_defined(gid, "missingValue") else None
            out = []
            for lat, lon in points:
                key = grid + (round(lat, 4), round(lon, 4))
                if key not in self._index:
                    self._index[key] = ec.codes_grib_find_nearest(gid, lat, lon)[0].index
                v = ec.codes_get_double_element(gid, "values", self._index[key])
                out.append(math.nan if missing is not None and v == missing else v)
            return out
        finally:
            ec.codes_release(gid)


class RRFS:
    def __init__(self, http: Http, cache=None, base_url: str = BASE_URL, grib: GribPoint | None = None,
                 points: list[tuple[float, float]] | None = None):
        self.http = http
        self.cache = cache  # object with get_point(url, field, lat, lon) / put_point(...)
        self.base_url = base_url.rstrip("/")
        self._grib = grib
        self._idx: dict[str, list[tuple[int, str]] | None] = {}
        # Every location to read from each downloaded message, e.g. all paper-traded cities. All
        # cities share the same NOAA files, so one download fills the cache for all of them.
        self.points = list(dict.fromkeys(points or []))

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
        return self._download(url, field, rng[0], rng[1], lat, lon)

    def points_from(self, url: str, field: str, lat: float, lon: float, init: datetime, not_before: datetime) -> list[float] | None:
        """Values of every time step of `field` in a multi-step file valid at or after `not_before`."""
        entries = self.index(url)
        if not entries:
            return None
        out = []
        for start, end, desc in byte_ranges(entries, field):
            minutes = lead_minutes(desc)
            if minutes is None or init + timedelta(minutes=minutes) < not_before:
                continue
            key = f"{field}@{minutes}min"  # cache key per time step
            hit = self.cache.get_point(url, key, lat, lon) if self.cache is not None else None
            v = hit if hit is not None else self._download(url, key, start, end, lat, lon)
            if v is not None:
                out.append(v)
        return out

    def _download(self, url: str, key: str, start: int, end: int | None, lat: float, lon: float) -> float | None:
        """Fetch one message, read every configured point from it, cache them, return (lat, lon)'s."""
        r = self.http.get(url, headers={"Range": f"bytes={start}-{'' if end is None else end}"})
        points = list(dict.fromkeys([(lat, lon), *self.points]))
        values_f = [kelvin_to_f(v) for v in self.grib.values(r.content, points)]
        if self.cache is not None:
            for (plat, plon), v in zip(points, values_f):
                if not math.isnan(v):
                    self.cache.put_point(url, key, plat, plon, v)
        return None if math.isnan(values_f[0]) else values_f[0]

    def choose_cycle(self, product: Product, start: datetime, end: datetime, now: datetime, member: int) -> datetime | None:
        """Newest cycle whose last needed file has been published."""
        for init in candidate_cycles(start, end, product, now):
            last = needed_hours(start, end, init, product.interval_max)[-1]
            if self.index(self.url(product, init, last, member)) is not None:
                return init
        return None

    def published(self, product: Product, init: datetime, fxx: int, member: int = 0) -> bool:
        return self.index(self.url(product, init, fxx, member)) is not None

    def _values(self, product: Product, init: datetime, hours: list[int], member: int, station: Station,
                not_before: datetime, kind: str = "high") -> list[float] | None:
        values = []
        field = product.field_for(kind)
        for fxx in hours:
            url = self.url(product, init, fxx, member)
            if product.multi:
                got = self.points_from(url, field, station.lat, station.lon, init, not_before)
            else:
                v = self.point(url, field, station.lat, station.lon)
                got = None if v is None else [v]
            if got is None:
                log.warning("RRFS f%03d missing for %s member %d; skipping this member", fxx, init, member)
                return None
            values += got
        return values

    def _hours(self, product: Product, init: datetime, start: datetime, end: datetime, since: datetime | None) -> list[int]:
        hours = needed_hours(start, end, init, product.interval_max)
        if since is not None:
            # A file/hour counts if it ends after `since` (interval products) or is at/after it.
            hours = [f for f in hours if init + timedelta(hours=f) > since or
                     (not product.interval_max and init + timedelta(hours=f) == since)]
        return [f for f in hours if (1 if product.interval_max else 0) <= f <= product.max_fxx]

    def member_max(self, product: Product, station: Station, day: date, member: int, now: datetime,
                   since: datetime | None = None, kind: str = "high") -> tuple[float, datetime] | None:
        """(daily max °F, cycle used), or None if no published cycle covers the climate day.

        With `since` (same day): the max over the rest of the day only (-inf if none is left). For
        the deterministic model, the newest published HOURLY run's 15-minute output covers as much of
        that as it reaches (18 h); any later hours come from the newest 6-hourly run that reaches the
        end of the day. So the afternoon, which decides the high, uses data at most ~2 hours old.
        """
        start, end = climate_day_window(day, station.tz)
        remaining = since is not None and since > start
        done = -math.inf if kind == "high" else math.inf  # nothing left to change the extreme
        pick = max if kind == "high" else min
        if remaining and since >= end:
            return done, None
        frm = since if remaining else start

        # The newest run of `product` that covers everything left and is fully published.
        base = None
        for init in candidate_cycles(frm, end, product, now):
            hours = self._hours(product, init, start, end, since if remaining else None)
            if not hours:
                return done, init
            if self.published(product, init, hours[-1], member):
                base = (init, hours)
                break

        if remaining and product is DETERMINISTIC:
            for hinit in recent_hourly_runs(since, now):
                if base and hinit <= base[0]:
                    break  # no hourly run newer than the 6-hourly one
                h_end = min(end, hinit + timedelta(hours=SUBHOURLY.max_fxx))
                h_hours = self._hours(SUBHOURLY, hinit, start, h_end, since)
                if not h_hours or not self.published(SUBHOURLY, hinit, h_hours[-1]):
                    continue
                values = self._values(SUBHOURLY, hinit, h_hours, member, station, since, kind)
                if values is None:
                    continue
                if h_end < end:  # the evening tail beyond the hourly run's reach
                    if base is None:
                        continue
                    tail = [f for f in base[1] if base[0] + timedelta(hours=f) > h_end]
                    more = self._values(DETERMINISTIC, base[0], tail, member, station, h_end, kind)
                    if more is None:
                        continue
                    values += more
                return (pick(values) if values else done), hinit

        if base is None:
            return None
        values = self._values(product, base[0], base[1], member, station, frm, kind)
        if values is None:
            return None
        return (pick(values) if values else done), base[0]

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

    def member_maxes(self, station: Station, day: date, models: list[Model], now: datetime | None = None,
                     since: datetime | None = None, kind: str = "high") -> dict[tuple[str, int], float]:
        now = now or datetime.now(timezone.utc)
        out = {}
        for m in models:
            if m.source == "rrfs":
                got = self.member_max(DETERMINISTIC, station, day, 0, now, since, kind)
                if got:
                    out[(m.id, 0)] = got[0]
            elif m.source == "refs":
                for member in m.members or range(1, 6):
                    got = self.member_max(ENSEMBLE, station, day, member, now, since, kind)
                    if got:
                        out[(m.id, member)] = got[0]
        return out
