"""Turn pooled ensemble member maxima into a smooth distribution of the daily high."""

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

from .brackets import Bracket


def member_weights(members_per_model: dict[str, int], model_weights: dict[str, float]) -> dict[str, float]:
    """Weight of ONE member of each model: each model's weight is split evenly across its members,
    then normalised so all members together sum to 1."""
    active = {m: model_weights.get(m, 0.0) for m, n in members_per_model.items() if n > 0}
    total = sum(active.values())
    if total <= 0:
        raise ValueError("no model with members and a positive weight")
    return {m: w / total / members_per_model[m] for m, w in active.items()}


def weighted_quantile(x: np.ndarray, w: np.ndarray, q: float) -> float:
    order = np.argsort(x)
    cw = np.cumsum(w[order])
    return float(np.interp(q * cw[-1], cw, x[order]))


def silverman_bandwidth(x: np.ndarray, w: np.ndarray) -> float:
    mean = np.average(x, weights=w)
    sd = math.sqrt(np.average((x - mean) ** 2, weights=w))
    iqr = weighted_quantile(x, w, 0.75) - weighted_quantile(x, w, 0.25)
    spread = min(sd, iqr / 1.34) if iqr > 0 else sd
    n_eff = 1.0 / np.sum(w**2)
    return max(0.9 * spread * n_eff ** (-0.2), 0.3)  # floor: whole-degree data never pins the high exactly


@dataclass
class HighDistribution:
    """Weighted Gaussian mixture over (adjusted) member maxima, optionally truncated below."""

    centers: np.ndarray
    weights: np.ndarray
    bandwidth: float
    floor_f: float = -math.inf  # the high can't end below this (observed so far)

    @classmethod
    def from_members(
        cls,
        values: np.ndarray,
        weights: np.ndarray,
        bias_f: float = 0.0,
        spread_inflation: float = 1.0,
        bandwidth_f: float | None = None,
    ) -> "HighDistribution":
        values = np.asarray(values, float)
        weights = np.asarray(weights, float)
        if len(values) == 0:
            raise ValueError("no ensemble members")
        weights = weights / weights.sum()
        mean = np.average(values, weights=weights)
        # Raw ensembles are usually underdispersed: widen around the pooled mean, then shift.
        centers = mean + spread_inflation * (values - mean) + bias_f
        bw = bandwidth_f if bandwidth_f is not None else silverman_bandwidth(centers, weights)
        return cls(centers, weights, bw)

    def _raw_cdf(self, x: float) -> float:
        if x == math.inf:
            return 1.0
        if x == -math.inf:
            return 0.0
        return float(np.sum(self.weights * norm.cdf((x - self.centers) / self.bandwidth)))

    def cdf(self, x: float) -> float:
        if x <= self.floor_f:
            return 0.0
        base = self._raw_cdf(self.floor_f)
        mass = 1.0 - base
        if mass <= 1e-12:
            return 1.0 if x >= self.floor_f else 0.0  # everything was below the floor: high = floor
        return (self._raw_cdf(x) - base) / mass

    def conditioned_on_floor(self, floor_f: float) -> "HighDistribution":
        return HighDistribution(self.centers, self.weights, self.bandwidth, max(self.floor_f, floor_f))

    def mean(self) -> float:
        if self.floor_f == -math.inf:
            return float(np.sum(self.weights * self.centers))
        # Numerical mean of the truncated mixture.
        grid = np.linspace(min(self.floor_f, self.centers.min()) - 1, self.centers.max() + 6 * self.bandwidth, 4000)
        cdf = np.array([self.cdf(g) for g in grid])
        return float(grid[-1] - np.trapezoid(cdf, grid))

    def bracket_probs(self, brackets: list[Bracket]) -> list[float]:
        return [self.cdf(b.upper_f) - self.cdf(b.lower_f) for b in brackets]
