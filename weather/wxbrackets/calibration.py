"""Score logged forecasts against official highs: Brier score, reliability, and bias."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .brackets import Bracket
from .db import DB


@dataclass
class CalibrationResult:
    runs: int
    days: int
    brier: float | None  # multi-category Brier: sum over brackets of (p - outcome)^2, averaged over runs
    mean_bias_f: float | None  # raw pooled ensemble mean minus official high (positive = too warm)
    mean_abs_err_f: float | None
    dist_bias_f: float | None  # final distribution mean minus official high
    nws_bias_f: float | None
    reliability: pd.DataFrame
    per_model: pd.DataFrame  # model, runs, bias_f, mae_f: each model's own mean vs. the official high


def reliability_table(pairs: list[tuple[float, int]], bins: int = 10) -> pd.DataFrame:
    """Bin bracket probabilities; compare the average forecast with how often those brackets hit."""
    df = pd.DataFrame(pairs, columns=["forecast", "hit"])
    if df.empty:
        return pd.DataFrame(columns=["bin", "count", "mean_forecast", "observed_freq"])
    edges = np.linspace(0, 1, bins + 1)
    df["bin"] = pd.cut(df["forecast"], edges, include_lowest=True)
    out = df.groupby("bin", observed=True).agg(count=("hit", "size"), mean_forecast=("forecast", "mean"), observed_freq=("hit", "mean"))
    return out.reset_index()


def calibrate(db: DB, station: str, min_lead: float | None = None, max_lead: float | None = None) -> CalibrationResult:
    runs = db.scored_runs(station, min_lead, max_lead)
    briers, pairs, raw_err, dist_err, nws_err = [], [], [], [], []
    for r in runs:
        official = r["official_f"]
        rows = db.brackets_for(r["run_id"])
        if rows:
            hits = [int(Bracket(b["label"], b["low_f"], b["high_f"]).contains(official)) for b in rows]
            probs = [b["prob"] for b in rows]
            briers.append(sum((p - h) ** 2 for p, h in zip(probs, hits)))
            pairs += list(zip(probs, hits))
        raw_err.append(r["pooled_mean_f"] - official)
        dist_err.append(r["dist_mean_f"] - official)
        if r["nws_forecast_high_f"] is not None:
            nws_err.append(r["nws_forecast_high_f"] - official)

    def mean(xs):
        return float(np.mean(xs)) if xs else None

    rows = [dict(r) for r in db.member_errors(station, min_lead, max_lead)]
    if rows:
        errs = pd.DataFrame(rows).assign(err=lambda d: d.mean_max_f - d.official_f)
        per_model = (
            errs.groupby("model")
            .agg(runs=("err", "size"), bias_f=("err", "mean"), mae_f=("err", lambda e: e.abs().mean()))
            .reset_index()
        )
    else:
        per_model = pd.DataFrame(columns=["model", "runs", "bias_f", "mae_f"])

    return CalibrationResult(
        runs=len(runs),
        days=len({r["target_date"] for r in runs}),
        brier=mean(briers),
        mean_bias_f=mean(raw_err),
        mean_abs_err_f=mean([abs(e) for e in raw_err]),
        dist_bias_f=mean(dist_err),
        nws_bias_f=mean(nws_err),
        reliability=reliability_table(pairs),
        per_model=per_model,
    )
