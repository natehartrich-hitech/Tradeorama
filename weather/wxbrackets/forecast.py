"""Build a high-temperature distribution and bracket probabilities from member maxima."""

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from .brackets import Bracket
from .climate_day import climate_day_window
from .config import Config, Station
from .distribution import HighDistribution, member_weights


@dataclass
class Forecast:
    station: Station
    day: date
    created: datetime
    lead_hours: float
    members: list[tuple[str, int, float, float]]  # (model, member, max_f, weight)
    per_model: dict[str, tuple[int, float]]  # model -> (members used, mean max)
    pooled_mean_f: float
    dist: HighDistribution
    brackets: list[Bracket]
    probs: list[float]
    observed_max_f: float | None = None
    nws_forecast_high_f: float | None = None


def build_forecast(
    cfg: Config,
    station: Station,
    day: date,
    maxes: dict[tuple[str, int], float],
    brackets: list[Bracket],
    now: datetime,
    observed_max_f: float | None = None,
    nws_forecast_high_f: float | None = None,
) -> Forecast:
    if not maxes:
        raise ValueError(f"no ensemble member covers the {day} climate day; is the date within the models' range?")
    counts: dict[str, int] = {}
    for model, _ in maxes:
        counts[model] = counts.get(model, 0) + 1
    w_member = member_weights(counts, {m.id: m.weight for m in cfg.models})

    keys = [k for k in sorted(maxes) if k[0] in w_member]
    values = np.array([maxes[k] for k in keys])
    weights = np.array([w_member[k[0]] for k in keys])
    d = cfg.distribution
    dist = HighDistribution.from_members(values, weights, d.bias_f, d.spread_inflation, d.bandwidth_f)
    if observed_max_f is not None:
        dist = dist.conditioned_on_floor(observed_max_f - d.obs_tolerance_f)

    per_model = {
        m: (counts[m], float(np.mean([maxes[k] for k in keys if k[0] == m]))) for m in sorted(w_member)
    }
    start, _ = climate_day_window(day, station.tz)
    return Forecast(
        station=station,
        day=day,
        created=now,
        lead_hours=(start - now).total_seconds() / 3600,
        members=[(m, n, maxes[(m, n)], w_member[m]) for m, n in keys],
        per_model=per_model,
        pooled_mean_f=float(np.average(values, weights=weights)),
        dist=dist,
        brackets=brackets,
        probs=dist.bracket_probs(brackets),
        observed_max_f=observed_max_f,
        nws_forecast_high_f=nws_forecast_high_f,
    )


def run_record(f: Forecast, cfg: Config) -> dict:
    return {
        "created_utc": f.created.isoformat(timespec="seconds"),
        "station": f.station.code,
        "target_date": f.day.isoformat(),
        "lead_hours": round(f.lead_hours, 2),
        "bias_f": cfg.distribution.bias_f,
        "spread_inflation": cfg.distribution.spread_inflation,
        "bandwidth_f": f.dist.bandwidth,
        "pooled_mean_f": f.pooled_mean_f,
        "dist_mean_f": f.dist.mean(),
        "observed_max_f": f.observed_max_f,
        "nws_forecast_high_f": f.nws_forecast_high_f,
    }
