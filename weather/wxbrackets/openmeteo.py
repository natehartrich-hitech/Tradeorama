"""Open-Meteo Ensemble API: hourly 2 m temperature for every member of several ensemble models."""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from .climate_day import climate_day_window
from .config import Config, Model, Station
from .http import Http

VARIABLE = "temperature_2m"
log = logging.getLogger(__name__)


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
    out, unknown = [], []
    for key, values in hourly.items():
        if key == "time":
            continue
        parsed = parse_member_key(key, variable, model_ids)
        if parsed is None:
            if key.startswith(variable):
                unknown.append(key)
            continue
        model, member = parsed
        if model is None:
            if len(model_ids) != 1:
                raise ValueError(f"key {key!r} has no model id, but several models were requested")
            model = model_ids[0]
        arr = np.array([np.nan if v is None else float(v) for v in values], dtype=float)
        out.append(MemberSeries(model, member, times, arr))
    if unknown:
        log.warning("ignored %d %s keys that don't match the requested models %s, e.g. %s",
                    len(unknown), variable, model_ids, unknown[:3])
    return sorted(out, key=lambda s: (s.model, s.member))


def member_daily_max(
    series: MemberSeries, start: datetime, end: datetime, min_valid: int, edge_tolerance_hours: float = 3.0
) -> float | None:
    """Max over hourly values with start <= t <= end (both climate-day boundaries count).

    None unless the member has at least `min_valid` values AND its data reaches within
    `edge_tolerance_hours` of both ends of the day. A run that stops mid-afternoon (HRRR's
    18-hour runs, or any model at the end of its range) would otherwise report a max that
    is too low.
    """
    lo, hi = int(start.timestamp()), int(end.timestamp())
    mask = (series.times >= lo) & (series.times <= hi) & ~np.isnan(series.values)
    times, vals = series.times[mask], series.values[mask]
    if len(vals) < min_valid:
        return None
    slack = edge_tolerance_hours * 3600
    if times.min() > lo + slack or times.max() < hi - slack:
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

    def url(self, source: str) -> str:
        return self.cfg.open_meteo_forecast_url if source == "forecast" else self.cfg.open_meteo_url

    def fetch(self, station: Station, day: date, model_ids: list[str], source: str = "ensemble") -> dict:
        return self.http.json(self.url(source), self.request_params(station, day, model_ids))

    def member_maxes(self, station: Station, day: date, models: list[Model]) -> tuple[dict[tuple[str, int], float], dict]:
        """{(model, member): daily max °F} for members covering the climate day, plus raw payloads by model.

        One request per model. A combined multi-model request labels its keys differently from
        the model ids it was asked for (seen live in November 2026: 143 keys, none matched), so
        each model is fetched alone, where keys are plainly temperature_2m / temperature_2m_memberNN.
        """
        start, end = climate_day_window(day, station.tz)
        d = self.cfg.distribution
        maxes, payloads = {}, {}
        for m in models:
            if m.source not in ("ensemble", "forecast"):
                continue
            payload = payloads[m.id] = self.fetch(station, day, [m.id], m.source)
            for s in parse_members(payload, [m.id]):
                mx = member_daily_max(s, start, end, d.min_valid_hours, d.edge_tolerance_hours)
                if mx is not None:
                    maxes[(s.model, s.member)] = mx
        return maxes, payloads
