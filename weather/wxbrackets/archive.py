"""Archived model runs on AWS, for backtesting: HRRR, RRFS, REFS, GEFS and the ECMWF ensemble.

Each model is described by where its files live, which runs exist, how long after a run
starts it is fully published (a conservative estimate, so a backtest never uses a run before
it could really have been seen), and which 2 m temperature messages to read:
  - snapshots: instantaneous temperature at regular steps;
  - intervals: max/min over the preceding few hours (catches peaks between snapshots).
A member's daily value is the max (highs) or min (lows) over every snapshot and interval that
lies inside the climate day.

Archives checked November 2026: HRRR from 2014, GEFS from 2017, ECMWF open data from Jan 2023,
RRFS from Aug 12 2026, REFS from Sep 9 2026.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from .rrfs import parse_idx


@dataclass(frozen=True)
class Piece:
    """One message to read: a snapshot (start == end) or an interval (start, end]."""

    url: str
    key: str  # cache key: the field, plus the member for multi-member files
    selector: tuple  # how to find it in the file's index
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ArchiveModel:
    id: str
    name: str
    weight: float
    members: tuple
    cycles: tuple  # run start hours (UTC) used
    max_fxx: int
    delay_h: float  # hours after the run starts before it's fully published (conservative)
    first_day: date  # first day in the archive
    path: Callable[[datetime, int, object], str]  # (init, forecast hour, member) -> URL
    index: str  # "wgrib2" (.idx) or "ecmwf" (.index, JSON lines)
    snapshot: str | None = None  # e.g. "TMP:2 m above ground" or "2t"
    snapshot_every: int = 1
    interval: dict = field(default_factory=dict)  # kind -> field, e.g. {"high": "TMAX:2 m above ground"}
    interval_hours: Callable[[int], int] = lambda fxx: 1  # length of the interval ending at fxx
    interval_every: int = 1

    def pieces(self, init: datetime, member, start: datetime, end: datetime, kind: str) -> list[Piece]:
        out = []
        for fxx in range(1, self.max_fxx + 1):
            t = init + timedelta(hours=fxx)
            if t < start or t - timedelta(hours=6) > end:
                continue
            url = self.path(init, fxx, member)
            if self.snapshot and fxx % self.snapshot_every == 0 and start <= t <= end:
                out.append(Piece(url, self._key(self.snapshot, member), self._sel(self.snapshot, member, fxx), t, t))
            fld = self.interval.get(kind)
            if fld and fxx % self.interval_every == 0:
                a = t - timedelta(hours=self.interval_hours(fxx))
                if start <= a and t <= end:
                    out.append(Piece(url, self._key(fld, member), self._sel(fld, member, fxx), a, t))
        return out

    def _key(self, fld: str, member) -> str:
        # "@land": read at the nearest land grid point (values cached before that fix are ignored).
        return (f"{fld}#{member}" if self.index == "ecmwf" else fld) + "@land"

    def _sel(self, fld: str, member, fxx: int) -> tuple:
        return ("ecmwf", fld, str(member), str(fxx)) if self.index == "ecmwf" else ("wgrib2", fld)

    def run_for(self, decision: datetime, latest_end: datetime) -> datetime | None:
        """Newest run published by `decision` that reaches `latest_end`."""
        t = decision.replace(minute=0, second=0, microsecond=0)
        for _ in range(72):
            if (t.hour in self.cycles and t + timedelta(hours=self.delay_h) <= decision
                    and t + timedelta(hours=self.max_fxx) >= latest_end and t.date() >= self.first_day):
                return t
            t -= timedelta(hours=1)
        return None


def byte_range_for(index_text: str, selector: tuple) -> tuple[int, int | None] | None:
    """Start and inclusive end byte of the selected message, from a .idx or ECMWF .index file."""
    if selector[0] == "ecmwf":
        _, param, number, step = selector
        for line in index_text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("param") == param and str(row.get("number")) == number and str(row.get("step")) == step:
                return row["_offset"], row["_offset"] + row["_length"] - 1
        return None
    var, _, level = selector[1].partition(":")
    entries = parse_idx(index_text)
    for i, (offset, desc) in enumerate(entries):
        bits = desc.split(":")
        if len(bits) >= 3 and bits[1] == var and bits[2] == level:
            return offset, (entries[i + 1][0] - 1 if i + 1 < len(entries) else None)
    return None


def index_url(url: str, kind: str) -> str:
    return url[: -len(".grib2")] + ".index" if kind == "ecmwf" else url + ".idx"


S3 = "https://{bucket}.s3.amazonaws.com/"


def _hrrr(init, fxx, member):
    return S3.format(bucket="noaa-hrrr-bdp-pds") + f"hrrr.{init:%Y%m%d}/conus/hrrr.t{init:%H}z.wrfsfcf{fxx:02d}.grib2"


def _rrfs(init, fxx, member):
    return S3.format(bucket="noaa-rrfs-ops-pds") + f"rrfs.{init:%Y%m%d}/{init:%H}/rrfs.t{init:%H}z.2dfld.3km.f{fxx:03d}.conus.grib2"


def _refs(init, fxx, member):
    return (S3.format(bucket="noaa-rrfs-ops-pds")
            + f"rrfsens.{init:%Y%m%d}/{init:%H}/m{member:03d}/rrfs.t{init:%H}z.m{member:03d}.2dfldnomads.3km.f{fxx:03d}.conus.grib2")


def _gefs(init, fxx, member):
    name = "gec00" if member == 0 else f"gep{member:02d}"
    return (S3.format(bucket="noaa-gefs-pds")
            + f"gefs.{init:%Y%m%d}/{init:%H}/atmos/pgrb2ap5/{name}.t{init:%H}z.pgrb2a.0p50.f{fxx:03d}")


def _ecmwf(init, fxx, member):
    return (S3.format(bucket="ecmwf-forecasts")
            + f"{init:%Y%m%d}/{init:%H}z/ifs/0p25/enfo/{init:%Y%m%d%H}0000-{fxx}h-enfo-ef.grib2")


MODELS = {
    m.id: m
    for m in [
        # HRRR: hourly runs, 48 h at 00/06/12/18Z; the archive has hourly 2 m temperature only.
        ArchiveModel("hrrr", "NOAA HRRR", 0.5, (0,), (0, 6, 12, 18), 48, 2, date(2014, 7, 30), _hrrr, "wgrib2",
                     snapshot="TMP:2 m above ground"),
        # RRFS: max/min over each hour.
        ArchiveModel("rrfs", "NOAA RRFS", 0.5, (0,), (0, 6, 12, 18), 84, 3, date(2026, 8, 12), _rrfs, "wgrib2",
                     interval={"high": "TMAX:2 m above ground", "low": "TMIN:2 m above ground"}),
        ArchiveModel("refs", "NOAA REFS", 1.0, (1, 2, 3, 4, 5), (0, 6, 12, 18), 60, 5, date(2026, 9, 9), _refs,
                     "wgrib2", snapshot="TMP:2 m above ground"),
        # GEFS 0.5 deg: 3-hourly temperature; max/min over (f-3, f] when f is 3 past a 6-hour mark, else (f-6, f].
        ArchiveModel("gefs", "NOAA GEFS (0.5°)", 1.0, tuple(range(31)), (0, 6, 12, 18), 72, 6, date(2017, 1, 1), _gefs,
                     "wgrib2", snapshot="TMP:2 m above ground", snapshot_every=3,
                     interval={"high": "TMAX:2 m above ground", "low": "TMIN:2 m above ground"},
                     interval_hours=lambda fxx: 3 if fxx % 6 == 3 else 6, interval_every=3),
        # ECMWF ensemble (open data, 50 perturbed members): 3-hourly temperature and 3-hour max/min.
        ArchiveModel("ecmwf", "ECMWF IFS ensemble", 1.0, tuple(range(1, 51)), (0, 12), 72, 9, date(2023, 1, 18),
                     _ecmwf, "ecmwf", snapshot="2t", snapshot_every=3,
                     interval={"high": "mx2t3", "low": "mn2t3"}, interval_hours=lambda fxx: 3, interval_every=3),
    ]
}

# Rough message sizes (MB), for download estimates.
MESSAGE_MB = {"hrrr": 1.4, "rrfs": 1.7, "refs": 1.7, "gefs": 0.14, "ecmwf": 0.65}

# Land-sea masks (url, selector, index kind) per model, so stations read the nearest LAND point.
# GEFS 0.5° has no mask of its own; GFS 0.5° shares its grid. REFS shares RRFS's 3 km grid.
_RRFS_MASK = (S3.format(bucket="noaa-rrfs-ops-pds") + "rrfs.20261020/00/rrfs.t00z.2dfld.3km.f001.conus.grib2",
              ("wgrib2", "LAND:surface"), "wgrib2")
MASKS = {
    "hrrr": (_hrrr(datetime(2026, 10, 20), 0, 0), ("wgrib2", "LAND:surface"), "wgrib2"),
    "rrfs": _RRFS_MASK,
    "refs": _RRFS_MASK,
    "gefs": (S3.format(bucket="noaa-gfs-bdp-pds") + "gfs.20261020/00/atmos/gfs.t00z.pgrb2.0p50.f000",
             ("wgrib2", "LAND:surface"), "wgrib2"),
    "ecmwf": (_ecmwf(datetime(2026, 10, 20), 24, 1), ("ecmwf", "lsm", "1", "24"), "ecmwf"),
}
