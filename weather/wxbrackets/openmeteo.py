"""Open-Meteo Ensemble API: hourly 2 m temperature for every member of several ensemble models."""

import re
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from .climate_day import climate_day_window
from .config import Config, Station
from .http import Http

VARIABLE = "temperature_2m"


@dataclass
class MemberSeries:
    model: str
    member: int  # 0 = control run
    times: np.ndarray  # unix seconds, UTC
    values: np.ndarray  # °F, NaN where missing


def parse_member_key(key: str, variable: str, model_ids: list[str]) -> tuple[str | None, int] | None:
    """Split a response key into (model, member). Returns None for keys that aren't this variable.

    Open-Meteo names the control run `temperature_2m` and perturbed members
    `temperature_2m_member01`. When several models are requested each key also carries
    the model id. Both suffix orders are accepted, since the docs examples differ:
      temperature_2m_member03_gfs025   temperature_2m_gfs025_member03
    The model is None when the key has no model suffix (single-model requests).
    """
    if not key.startswith(variable):
        return None
    rest = key[len(variable):]
    if rest == "":
        return None, 0
    if not rest.startswith("_"):
        return None  # a different variable that shares the prefix, e.g. temperature_2m_max
    rest = rest[1:]
    # Longest model ids first, so e.g. "icon_seamless" wins over "icon".
    for model in sorted(model_ids, key=len, reverse=True):
        for pattern in (rf"^member(\d+)_{re.escape(model)}$", rf"^{re.escape(model)}_member(\d+)$"):
            m = re.match(pattern, rest)
            if m:
                return model, int(m.group(1))
        if rest == model:
            return model, 0
    m = re.match(r"^member(\d+)$", rest)
    if m:
        return None, int(m.group(1))
    return None


def parse_members(payload: dict, model_ids: list[str], variable: str = VARIABLE) -> list[MemberSeries]:
    hourly = payload.get("hourly") or {}
    times = np.asarray(hourly.get("time") or [], dtype=np.int64)
    out = []
    for key, values in hourly.items():
        if key == "time":
            continue
        parsed = parse_member_key(key, variable, model_ids)
        if parsed is None:
            continue
        model, member = parsed
        if model is None:
            if len(model_ids) != 1:
                raise ValueError(f"key {key!r} has no model id, but several models were requested")
            model = model_ids[0]
        arr = np.array([np.nan if v is None else float(v) for v in values], dtype=float)
        out.append(MemberSeries(model, member, times, arr))
    return sorted(out, key=lambda s: (s.model, s.member))


def member_daily_max(series: MemberSeries, start: datetime, end: datetime, min_valid: int) -> float | None:
    """Max over hourly values with start <= t <= end (both climate-day boundaries count)."""
    lo, hi = int(start.timestamp()), int(end.timestamp())
    mask = (series.times >= lo) & (series.times <= hi)
    vals = series.values[mask]
    vals = vals[~np.isnan(vals)]
    if len(vals) < min_valid:
        return None
    return float(vals.max())


class OpenMeteo:
    def __init__(self, cfg: Config, http: Http):
        self.cfg = cfg
        self.http = http

    def request_params(self, station: Station, day: date, model_ids: list[str]) -> dict:
        start, end = climate_day_window(day, station.tz)
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": VARIABLE,
            "models": ",".join(model_ids),
            "temperature_unit": "fahrenheit",
            "timeformat": "unixtime",
            # No timezone parameter: dates are UTC, and we convert times ourselves because
            # the API's offsets don't follow the standard-time climate day.
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
        }
        if self.cfg.open_meteo_key:
            params["apikey"] = self.cfg.open_meteo_key
        return params

    def fetch(self, station: Station, day: date, model_ids: list[str]) -> dict:
        return self.http.json(self.cfg.open_meteo_url, self.request_params(station, day, model_ids))

    def member_maxes(self, station: Station, day: date, model_ids: list[str]) -> tuple[dict[tuple[str, int], float], dict]:
        """{(model, member): daily max °F} for members covering the climate day, plus the raw payload."""
        payload = self.fetch(station, day, model_ids)
        start, end = climate_day_window(day, station.tz)
        maxes = {}
        for s in parse_members(payload, model_ids):
            mx = member_daily_max(s, start, end, self.cfg.distribution.min_valid_hours)
            if mx is not None:
                maxes[(s.model, s.member)] = mx
        return maxes, payload

