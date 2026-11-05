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
    per_model_lead: pd.DataFrame  # the same, split by lead: model, lead, runs, bias_f, mae_f
    market_by_lead: dict = None  # lead -> (runs, model Brier, market Brier)
    market_runs: int = 0  # runs that also captured Polymarket US prices
    model_brier_vs_market: float | None = None  # the model's Brier on just those runs
    market_brier: float | None = None  # the market's Brier on the same runs


LEADS = ["today (traded)", "today (shadow)", "next day", "2+ days"]


def lead_bucket(lead_hours: float, cutoff_hour: float = 12.0) -> str:
    """lead_hours = hours from the forecast to the start of the climate day (negative once it has begun).
    Same-day runs split at the paper trader's same-day cutoff (`cutoff_hour` hours into the day): runs
    after it aren't traded ("shadow"), and are scored on their own to decide whether to move it."""
    if lead_hours < 0:
        return "today (shadow)" if -lead_hours >= cutoff_hour else "today (traded)"
    return "next day" if lead_hours < 24 else "2+ days"


def market_probs(rows) -> list[float] | None:
    """Each bracket's mid (or the one quote available), normalised to sum to 1."""
    mids = []
    for r in rows:
        quotes = [q for q in (r["yes_bid"], r["yes_ask"]) if q is not None]
        if not quotes:
            return None
        mids.append(sum(quotes) / len(quotes))
    total = sum(mids)
    return [m / total for m in mids] if total > 0 else None


def reliability_table(pairs: list[tuple[float, int]], bins: int = 10) -> pd.DataFrame:
    """Bin bracket probabilities; compare the average forecast with how often those brackets hit."""
    df = pd.DataFrame(pairs, columns=["forecast", "hit"])
    if df.empty:
        return pd.DataFrame(columns=["bin", "count", "mean_forecast", "observed_freq"])
    edges = np.linspace(0, 1, bins + 1)
    df["bin"] = pd.cut(df["forecast"], edges, include_lowest=True)
    out = df.groupby("bin", observed=True).agg(count=("hit", "size"), mean_forecast=("forecast", "mean"), observed_freq=("hit", "mean"))
    return out.reset_index()


def calibrate(db: DB, station: str, min_lead: float | None = None, max_lead: float | None = None,
              cutoff_hour: float = 12.0, kind: str = "high") -> CalibrationResult:
    runs = db.scored_runs(station, min_lead, max_lead, kind)
    briers, pairs, raw_err, dist_err, nws_err = [], [], [], [], []
    model_vs, market_vs, by_lead = [], [], {}
    for r in runs:
        official = r["official_f"]
        rows = db.brackets_for(r["run_id"])
        if rows:
            hits = [int(Bracket(b["label"], b["low_f"], b["high_f"]).contains(official)) for b in rows]
            probs = [b["prob"] for b in rows]
            brier = sum((p - h) ** 2 for p, h in zip(probs, hits))
            briers.append(brier)
            pairs += list(zip(probs, hits))
            quotes = {q["label"]: q for q in db.market_prices(r["run_id"])}
            mkt = market_probs([quotes[b["label"]] for b in rows]) if set(quotes) == {b["label"] for b in rows} else None
            if mkt:
                market_brier = sum((p - h) ** 2 for p, h in zip(mkt, hits))
                model_vs.append(brier)
                market_vs.append(market_brier)
                by_lead.setdefault(lead_bucket(r["lead_hours"], cutoff_hour), []).append((brier, market_brier))
        raw_err.append(r["pooled_mean_f"] - official)
        dist_err.append(r["dist_mean_f"] - official)
        if r["nws_forecast_high_f"] is not None:
            nws_err.append(r["nws_forecast_high_f"] - official)

    def mean(xs):
        return float(np.mean(xs)) if xs else None

    rows = [dict(r) for r in db.member_errors(station, min_lead, max_lead, kind)]
    stats = dict(runs=("err", "size"), bias_f=("err", "mean"), mae_f=("err", lambda e: e.abs().mean()))
    if rows:
        errs = pd.DataFrame(rows).assign(err=lambda d: d.mean_max_f - d.official_f,
                                         lead=lambda d: d.lead_hours.map(lambda h: lead_bucket(h, cutoff_hour)))
        per_model = errs.groupby("model").agg(**stats).reset_index()
        per_model_lead = errs.groupby(["model", "lead"]).agg(**stats).reset_index()
    else:
        per_model = pd.DataFrame(columns=["model", "runs", "bias_f", "mae_f"])
        per_model_lead = pd.DataFrame(columns=["model", "lead", "runs", "bias_f", "mae_f"])

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
        per_model_lead=per_model_lead,
        market_by_lead={lead: (len(v), float(np.mean([a for a, _ in v])), float(np.mean([b for _, b in v])))
                        for lead, v in by_lead.items()},
        market_runs=len(market_vs),
        model_brier_vs_market=mean(model_vs),
        market_brier=mean(market_vs),
    )
