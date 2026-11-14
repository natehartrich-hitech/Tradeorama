"""Scores the market-making test against the rules fixed before the run (PROJECT_NOTES.md, "Market-making test")."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import rewards as rw
from . import sim
from .store import Store

DECIDING_SIZE = 100
SIZES = (25, 50, 100, 200)
CAP_MULTIPLE = 3  # stop adding to a side at 3x the quote size
MIN_WEEKLY_RETURN = 0.01
MIN_DAILY_NET = 10.0
MIN_POSITIVE_DAYS = 3
MIN_FILLS = 30


@dataclass
class MarketResult:
    ticker: str
    series: str
    res: sim.Result
    pnl: float  # what the inventory is worth minus what it cost, fees included
    markouts: list[float] = field(default_factory=list)
    day_net: dict = field(default_factory=dict)  # day -> rewards + P&L of that day's fills

    @property
    def net(self) -> float:
        return self.res.rewards + self.pnl


def load(store: Store, ticker: str):
    books, prev = [], ((), ())
    for r in store.books(ticker):
        if r["yes"] == "=" and r["no"] == "=":
            yes, no = prev  # unchanged since the previous snapshot
        else:
            yes, no = tuple(map(tuple, json.loads(r["yes"]))), tuple(map(tuple, json.loads(r["no"])))
        books.append(sim.Book(r["ts"], yes, no))
        prev = (yes, no)
    trades = [sim.Trade(r["ts"], r["yes_price"], r["count"], bool(r["taker_yes"])) for r in store.trades(ticker)]
    programs = [rw.Program(r["start_ts"], r["end_ts"], r["reward"], r["target"], r["discount"])
                for r in store.programs(ticker)]
    return books, trades, programs


def score(store: Store, sizes=SIZES) -> dict[float, list[MarketResult]]:
    """Every followed market, simulated at each size. Each market's data is read once."""
    started = store.get("started") or 0
    day_of = lambda ts: int((ts - started) // 86400) + 1
    out: dict[float, list[MarketResult]] = {s: [] for s in sizes}
    for m in store.all_markets():
        books, trades, programs = load(store, m["ticker"])
        if len(books) < 2:
            continue
        mid = next((b.mid_yes() for b in reversed(books) if b.mid_yes() is not None), None)
        for size in sizes:
            res = sim.simulate(books, trades, programs, size, CAP_MULTIPLE * size, m["maker_coef"] or 0.0, day_of)
            pnl = sim.value(res, books, m["result"]) - res.cost["yes"] - res.cost["no"]
            day_net = dict(res.by_day)
            for f in res.fills:  # each fill's P&L goes to the day it happened, valued like the position at the end
                if m["result"] in ("yes", "no"):
                    worth = 1.0 if f.side == m["result"] else 0.0
                elif mid is not None:
                    worth = mid if f.side == "yes" else 1 - mid
                else:
                    continue
                d = day_of(f.ts)
                day_net[d] = day_net.get(d, 0.0) + f.qty * (worth - f.price) - f.fee
            out[size].append(MarketResult(m["ticker"], m["series"], res, pnl, sim.markouts(res, books), day_net))
    return out


def summary(results: list[MarketResult]) -> dict:
    rewards = sum(r.res.rewards for r in results)
    pnl = sum(r.pnl for r in results)
    fees = sum(r.res.fees for r in results)
    days = max((r.res.seconds for r in results), default=0.0) / 86400
    capital = sum(r.res.avg_capital() for r in results)
    net = rewards + pnl
    return {"rewards": rewards, "pnl": pnl, "fees": fees, "net": net, "days": days, "capital": capital,
            "fills": sum(len(r.res.fills) for r in results),
            "per_day": net / days if days else 0.0,
            "weekly": (net / days * 7 / capital) if days and capital else 0.0,
            "markouts": [x for r in results for x in r.markouts]}


def report(store: Store, out=print) -> bool:
    c = store.counts()
    ends = store.get("ends")
    fmt = lambda v: f"{'-' if v < 0 else '+'}${abs(v):,.2f}"
    out("Market-making test (paper only; rules fixed Nov 14, 2026, before the run)")
    if not c["books"]:
        out("No data yet. Run: python -m tradeorama.mm run")
        return False
    out(f"  {c['markets']} markets followed ({c['active']} still active), {c['books']:,} book snapshots, "
        f"{c['trades']:,} trades, over {(c['last'] - c['first']) / 86400:.1f} days")
    if ends and c["last"] < ends - 3600:
        out("  Note: the 5-day window isn't over yet. This is an interim look; the verdict below isn't final.")

    by_size = score(store)
    out(f"\n  {'a side':16} {'rewards':>11} {'fill P&L':>11} {'fees':>9} {'net':>11} {'net a day':>10} "
        f"{'capital':>10} {'a week':>8} {'fills':>6}")
    for size, results in by_size.items():
        s = summary(results)
        label = f"{size} contracts" + (" *" if size == DECIDING_SIZE else "")
        out(f"  {label:16} {fmt(s['rewards']):>11} {fmt(s['pnl']):>11} {fmt(-s['fees']):>9} {fmt(s['net']):>11} "
            f"{fmt(s['per_day']):>10} {'$' + format(s['capital'], ',.0f'):>10} {100 * s['weekly']:+7.2f}% {s['fills']:6,}")
    out(f"  * the deciding size. Capital is the average committed to resting orders plus inventory.")

    res = by_size[DECIDING_SIZE]
    s = summary(res)
    if s["markouts"]:
        m = s["markouts"]
        out(f"\n  Adverse selection: an hour after a fill, the contract was worth {100 * sum(m) / len(m):+.2f}¢ "
            f"against its price, on average over {len(m):,} filled contracts")
    counted = sum(r.res.counted_seconds for r in res) / max(sum(r.res.seconds for r in res), 1)
    out(f"  Snapshots that counted for rewards (both sides deep enough): {100 * counted:.0f}% of the time")
    ranked = sorted(res, key=lambda r: r.net, reverse=True)
    out("  Best markets:  " + "; ".join(f"{r.ticker} {fmt(r.net)}" for r in ranked[:3]))
    out("  Worst markets: " + "; ".join(f"{r.ticker} {fmt(r.net)}" for r in ranked[-3:]))
    by_series: dict[str, float] = {}
    for r in res:
        by_series[r.series] = by_series.get(r.series, 0.0) + r.net
    out("  By series (top 8): " + "; ".join(f"{k} {fmt(v)}" for k, v in sorted(by_series.items(), key=lambda kv: -kv[1])[:8]))
    days: dict[int, float] = {}
    for r in res:
        for d, v in r.day_net.items():
            days[d] = days.get(d, 0.0) + v
    out("  By day: " + ", ".join(f"day {d}: {fmt(v)}" for d, v in sorted(days.items())))
    without_top = sum(r.net for r in ranked[3:])
    positive_days = sum(1 for d, v in days.items() if 1 <= d <= 5 and v > 0)

    rules = [
        (f"Net at least {100 * MIN_WEEKLY_RETURN:.0f}% of committed capital a week", f"{100 * s['weekly']:+.2f}%",
         s["weekly"] >= MIN_WEEKLY_RETURN),
        (f"Net at least ${MIN_DAILY_NET:.0f} a day", fmt(s["per_day"]), s["per_day"] >= MIN_DAILY_NET),
        ("Still positive without the best 3 markets", fmt(without_top), without_top > 0),
        (f"Positive on at least {MIN_POSITIVE_DAYS} of the 5 days", str(positive_days), positive_days >= MIN_POSITIVE_DAYS),
        (f"At least {MIN_FILLS} simulated fills", str(s["fills"]), s["fills"] >= MIN_FILLS),
    ]
    out(f"\nKill rules (at {DECIDING_SIZE} contracts a side)")
    for text, value, ok in rules:
        out(f"  {'PASS' if ok else 'FAIL'}  {text}: {value}")
    passed = all(ok for _, _, ok in rules)
    if s["fills"] < MIN_FILLS:
        out("Verdict: inconclusive: too few fills to measure adverse selection. Per the rules, extend the test.")
    elif passed:
        out("Verdict: passes. Next is the owner's decision on a small live test (needs a Kalshi account, API keys "
            "and an explicit go-ahead). Expect live results around half of these.")
    else:
        out("Verdict: fails a kill rule. Market making for Kalshi's rewards is dropped at this size.")
    return passed
