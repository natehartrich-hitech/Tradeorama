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
    observed_max_f: float | None = None  # observed extreme so far: max for highs, min for lows
    nws_forecast_high_f: float | None = None  # NWS point forecast for this kind (high or low)
    kind: str = "high"


def build_forecast(
    cfg: Config,
    station: Station,
    day: date,
    maxes: dict[tuple[str, int], float],
    brackets: list[Bracket],
    now: datetime,
    observed_max_f: float | None = None,
    nws_forecast_high_f: float | None = None,
    remaining: bool = False,
    kind: str = "high",
) -> Forecast:
    """`maxes`: each member's daily high (or, for kind="low", daily low). `observed_max_f`: the
    observed extreme so far (max for highs, min for lows). With `remaining`, `maxes` cover only the
    REST of the day (-inf/+inf if none is left), so a member's final value is the larger (highs) or
    smaller (lows) of that and the observation."""
    if not maxes:
        raise ValueError(f"no ensemble member covers the {day} climate day; is the date within the models' range?")
    counts: dict[str, int] = {}
    for model, _ in maxes:
        counts[model] = counts.get(model, 0) + 1
    w_member = member_weights(counts, {m.id: m.weight for m in cfg.models})

    keys = [k for k in sorted(maxes) if k[0] in w_member]
    weights = np.array([w_member[k[0]] for k in keys])
    d = cfg.distribution_for(kind)
    high = kind == "high"
    if remaining and observed_max_f is not None:
        obs = observed_max_f
        raw = np.array([maxes[k] for k in keys], dtype=float)
        finite = np.isfinite(raw)
        centers = np.full(len(keys), obs)
        bandwidth = d.bandwidth_f if d.bandwidth_f is not None else 0.5
        if finite.any():
            part = HighDistribution.from_members(raw[finite], weights[finite], d.bias_f, d.spread_inflation, d.bandwidth_f)
            centers[finite] = part.centers
            bandwidth = part.bandwidth
        # The day's high = max(observed so far, the member's forecast for the hours left); the low
        # = min(...). A member pinned at the observation only carries the uncertainty between hourly
        # observations and the official value (obs_tolerance_f), not the models' spread.
        pinned = centers <= obs if high else centers >= obs
        widths = np.where(pinned, d.obs_tolerance_f or 0.5, bandwidth)
        final = np.maximum(centers, obs) if high else np.minimum(centers, obs)
        dist = HighDistribution(final, weights / weights.sum(), widths)
        dist = (dist.conditioned_on_floor(obs - d.obs_tolerance_f) if high
                else dist.conditioned_on_ceiling(obs + d.obs_tolerance_f))
        clamp = max if high else min
        maxes = {k: clamp(maxes[k], obs) for k in keys}  # what's stored and reported per member
    else:
        dist = HighDistribution.from_members(np.array([maxes[k] for k in keys]), weights, d.bias_f,
                                             d.spread_inflation, d.bandwidth_f)
        if observed_max_f is not None:
            dist = (dist.conditioned_on_floor(observed_max_f - d.obs_tolerance_f) if high
                    else dist.conditioned_on_ceiling(observed_max_f + d.obs_tolerance_f))
    values = np.array([maxes[k] for k in keys])

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
        kind=kind,
    )


def run_record(f: Forecast, cfg: Config) -> dict:
    return {
        "created_utc": f.created.isoformat(timespec="seconds"),
        "kind": f.kind,
        "station": f.station.code,
        "target_date": f.day.isoformat(),
        "lead_hours": round(f.lead_hours, 2),
        "bias_f": cfg.distribution_for(f.kind).bias_f,
        "spread_inflation": cfg.distribution_for(f.kind).spread_inflation,
        "bandwidth_f": f.dist.kernel_f,
        "pooled_mean_f": f.pooled_mean_f,
        "dist_mean_f": f.dist.mean(),
        "observed_max_f": f.observed_max_f,
        "nws_forecast_high_f": f.nws_forecast_high_f,
    }
