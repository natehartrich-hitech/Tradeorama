"""Kalshi structural scan: do contracts priced 85–97¢ win more often than their price, after fees?

Pure functions, no I/O. Prices are dollars per $1 contract; times are Unix seconds.

The rules were written down before any results were seen (PROJECT_NOTES.md, "Structural scan"), so the
periods, price range, decision times and kill thresholds are constants here, not options.

How a bet is made, without looking ahead:
- Each market has a reference time fixed in advance: its close, when trading stops at a fixed time whatever
  happens (weather, index levels, the Fed decision), or else its scheduled expiration (markets that close early
  when their event happens, and sports). Measuring from the actual close of those would leak outcomes.
- At 24, 6 and 1 hours before it, the market must still be open, with a two-sided quote from the hourly candle
  that ended in the hour up to the decision. If YES's ask, or NO's ask (1 − YES bid), is 85–97¢, that side
  is bought.
- Taker: at the ask, plus Kalshi's taker fee, rounded up per 100-contract order.
- Maker: a bid 1¢ under the ask. It counts as filled only if a trade printed at or through it in the next
  hour, or strictly through it if the bid just joined an existing price level (queue position unknown).
  Fills come when the price moves against the bid, so this keeps the adverse selection a real maker faces.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

DECISION_HOURS = (24, 6, 1)
LOW, HIGH = 0.85, 0.97
FIT = ("2025-10-01", "2026-04-30")  # choose decision time and groups here
HOLDOUT = ("2026-05-01", "2026-08-31")  # score once, choices frozen
HOLDOUT_MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08")
STAKE = 100.0
ORDER_CONTRACTS = 100  # Kalshi rounds each order's fee up to the cent
MIN_FIT_BETS = 200  # a group needs this many fit-period bets to be chosen
MAX_GROUPS = 3
TAKER_COEF, MAKER_COEF = 0.07, 0.0175

# Kill rules: a hypothesis fails if any one fails on the holdout.
MIN_RETURN = 0.01  # per contract, after fees
MAX_LUCK_PCT = 5.0
MIN_POSITIVE_MONTHS = 3
MIN_WEEKLY_DOLLARS = 2000.0


@dataclass(frozen=True, slots=True)
class Candle:
    end: int
    bid: float | None  # YES bid at the end of the hour
    ask: float | None  # YES ask at the end of the hour
    low: float | None  # lowest YES trade price in the hour
    high: float | None
    volume: float  # contracts traded in the hour


@dataclass(frozen=True, eq=False)  # compared by identity: candles make equality slow
class Market:
    ticker: str
    event: str
    group: str  # category, or series for sports
    ref: float  # reference time fixed in advance (see __main__.reference)
    open_ts: float
    close_ts: float
    result: str  # "yes" or "no"
    taker_coef: float
    maker_coef: float
    weight: float  # 1 / chance this market was sampled, to scale volumes up to the whole series
    candles: tuple[Candle, ...]
    sports: bool = False

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.ref, tz=timezone.utc).strftime("%Y-%m-%d")

    @property
    def month(self) -> str:
        return self.day[:7]


@dataclass(frozen=True, slots=True)
class Bet:
    market: Market
    hours: int
    style: str  # "taker" or "maker"
    side: str  # "yes" or "no"
    price: float
    cost: float  # per contract, fee included
    won: bool
    hour_dollars: float  # traded in this market in the hour before the decision

    @property
    def contracts(self) -> float:
        return STAKE / self.cost

    @property
    def pnl(self) -> float:
        return (self.contracts if self.won else 0.0) - STAKE


def fee(coef: float, price: float, contracts: int = ORDER_CONTRACTS) -> float:
    """Per-contract fee for an order of `contracts`, with the order's total rounded up to the cent."""
    if coef <= 0:
        return 0.0
    total_cents = math.ceil(round(coef * contracts * price * (1 - price) * 100, 6))
    return total_cents / 100 / contracts


def fee_coefs(fee_type: str | None, multiplier: float | None) -> tuple[float, float]:
    """(taker, maker) coefficients for a series or an event's override."""
    m = 1.0 if multiplier is None else float(multiplier)
    maker = MAKER_COEF * m if fee_type == "quadratic_with_maker_fees" else 0.0
    return TAKER_COEF * m, maker


def quote_at(m: Market, t: float) -> Candle | None:
    best = None
    for c in m.candles:
        if t - 3600 < c.end <= t:
            best = c
    if best is None or best.bid is None or best.ask is None or not (0 < best.bid < best.ask < 1):
        return None
    return best


def next_hour(m: Market, t: float) -> Candle | None:
    for c in m.candles:
        if t < c.end <= t + 3600:
            return c
    return None


def bets_for(m: Market, hours: int, max_spread: float | None = None) -> list[Bet]:
    """max_spread: skip quotes wider than this (the first run had no such rule; see "Leads" below)."""
    t = m.ref - hours * 3600
    if not (m.open_ts <= t < m.close_ts) or m.result not in ("yes", "no"):
        return []
    q = quote_at(m, t)
    if q is None or (max_spread is not None and q.ask - q.bid > max_spread + 1e-9):
        return []
    out = []
    for side, ask, own_bid in (("yes", q.ask, q.bid), ("no", round(1 - q.bid, 4), round(1 - q.ask, 4))):
        if not (LOW - 1e-9 <= ask <= HIGH + 1e-9):
            continue
        won = m.result == side
        dollars = q.volume * ask
        out.append(Bet(m, hours, "taker", side, ask, ask + fee(m.taker_coef, ask), won, dollars))
        bid = round(ask - 0.01, 2)
        joins = bid <= own_bid + 1e-9  # the spread is 1¢: the bid joins the queue at the existing best bid
        nxt = next_hour(m, t)
        if nxt is None:
            continue
        if side == "yes":
            filled = nxt.low is not None and (nxt.low < bid - 1e-9 if joins else nxt.low <= bid + 1e-9)
        else:  # buying NO at `bid` is selling YES at 1 − bid
            filled = nxt.high is not None and (nxt.high > 1 - bid + 1e-9 if joins else nxt.high >= 1 - bid - 1e-9)
        if filled:
            out.append(Bet(m, hours, "maker", side, bid, bid + fee(m.maker_coef, bid), won, dollars))
    return out


def all_bets(markets: list[Market]) -> list[Bet]:
    return [b for m in markets for h in DECISION_HOURS for b in bets_for(m, h)]


def in_period(b: Bet, period: tuple[str, str]) -> bool:
    return period[0] <= b.market.day <= period[1]


@dataclass(frozen=True)
class Summary:
    bets: int
    events: int
    days: int
    ret: float | None  # profit per contract after fees
    avg_price: float | None
    win_rate: float | None
    luck_pct: float | None
    months: dict[str, float | None] = field(default_factory=dict)
    weekly_dollars: float = 0.0  # traded per week at qualifying prices, scaled up for sampling
    worst_day: float = 0.0  # P&L of the worst day at $100 a bet


def ret(bets: list[Bet]) -> float | None:
    n = sum(b.contracts for b in bets)
    return sum(b.pnl for b in bets) / n if n else None


def luck(bets: list[Bet], sims: int = 4000, seed: int = 1) -> float | None:
    """How often a strategy with no edge looks at least this good, resampling whole days.

    Days are the unit because bets on one day share news and, within an event, one outcome. The no-edge
    world keeps each day's spread of results but shifts it to zero profit per contract."""
    days: dict[str, list[float]] = {}
    for b in bets:
        d = days.setdefault(b.market.day, [0.0, 0.0])
        d[0] += b.pnl
        d[1] += b.contracts
    if len(days) < 5:
        return None
    observed = sum(p for p, _ in days.values()) / sum(c for _, c in days.values())
    centered = [(p - observed * c, c) for p, c in days.values()]
    rng = random.Random(seed)
    at_least = 0
    for _ in range(sims):
        sample = [rng.choice(centered) for _ in centered]
        at_least += sum(p for p, _ in sample) / sum(c for _, c in sample) >= observed - 1e-12
    return 100 * at_least / sims


def summarize(bets: list[Bet], period: tuple[str, str], months: tuple[str, ...] = ()) -> Summary:
    if not bets:
        return Summary(0, 0, 0, None, None, None, None, {m: None for m in months})
    first = datetime.fromisoformat(period[0])
    last = datetime.fromisoformat(period[1])
    weeks = ((last - first).days + 1) / 7
    by_day: dict[str, float] = {}
    for b in bets:
        by_day[b.market.day] = by_day.get(b.market.day, 0.0) + b.pnl
    n = sum(b.contracts for b in bets)
    return Summary(
        bets=len(bets),
        events=len({b.market.event for b in bets}),
        days=len(by_day),
        ret=ret(bets),
        avg_price=sum(b.price * b.contracts for b in bets) / n,
        win_rate=sum(b.contracts for b in bets if b.won) / n,
        luck_pct=luck(bets),
        months={m: ret([b for b in bets if b.market.month == m]) for m in months},
        weekly_dollars=sum(b.hour_dollars * b.market.weight for b in bets) / weeks,
        worst_day=min(by_day.values()),
    )


@dataclass(frozen=True)
class Hypothesis:
    name: str
    style: str
    picks: tuple[tuple[str | None, int], ...]  # (group or None for all, decision hours)
    note: str = ""

    def select(self, bets: list[Bet]) -> list[Bet]:
        keys = set(self.picks)
        return [b for b in bets if b.style == self.style and ((None, b.hours) in keys or (b.market.group, b.hours) in keys)]


def choose(fit: list[Bet]) -> list[Hypothesis]:
    """The three pre-set hypotheses, with their free choices made on the fit period only."""
    out = []
    taker = [b for b in fit if b.style == "taker"]
    pooled = {h: [b for b in taker if b.hours == h] for h in DECISION_HOURS}
    usable = {h: r for h, bs in pooled.items() if len(bs) >= MIN_FIT_BETS and (r := ret(bs)) is not None}
    if usable:
        h = max(usable, key=usable.get)
        out.append(Hypothesis("(a) taker, all groups", "taker", ((None, h),),
                              f"{h} h before, chosen on fit ({100 * usable[h]:+.2f}¢)"))
    else:
        out.append(Hypothesis("(a) taker, all groups", "taker", (), "not enough fit-period bets"))
    for name, style in (("(b) taker, best groups", "taker"), ("(c) maker, best groups", "maker")):
        cells: dict[tuple[str, int], list[Bet]] = {}
        for b in fit:
            if b.style == style:
                cells.setdefault((b.market.group, b.hours), []).append(b)
        scored = [(ret(bs), key) for key, bs in cells.items() if len(bs) >= MIN_FIT_BETS]
        good = sorted([(r, k) for r, k in scored if r is not None and r >= MIN_RETURN], reverse=True)[:MAX_GROUPS]
        note = ", ".join(f"{g} {h} h ({100 * r:+.2f}¢)" for r, (g, h) in good) or "no group cleared +1¢ on fit"
        out.append(Hypothesis(name, style, tuple(k for _, k in good), note))
    return out


@dataclass(frozen=True)
class Rule:
    text: str
    value: str
    passed: bool


def kill_rules(s: Summary) -> list[Rule]:
    positive = sum(1 for r in s.months.values() if r is not None and r > 0)
    return [
        Rule(f"Return after fees at least {100 * MIN_RETURN:+.0f}¢ per contract",
             "–" if s.ret is None else f"{100 * s.ret:+.2f}¢", s.ret is not None and s.ret >= MIN_RETURN),
        Rule(f"Luck check under {MAX_LUCK_PCT:.0f}%", "–" if s.luck_pct is None else f"{s.luck_pct:.1f}%",
             s.luck_pct is not None and s.luck_pct < MAX_LUCK_PCT),
        Rule(f"Positive in at least {MIN_POSITIVE_MONTHS} of {len(s.months)} months", f"{positive}",
             positive >= MIN_POSITIVE_MONTHS),
        Rule(f"At least ${MIN_WEEKLY_DOLLARS:,.0f} a week traded at qualifying prices", f"${s.weekly_dollars:,.0f}",
             s.weekly_dollars >= MIN_WEEKLY_DOLLARS),
    ]


BUCKETS = [(0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80),
           (0.80, 0.90), (0.90, 0.95), (0.95, 1.0)]


def calibration(markets: list[Market], period: tuple[str, str], hours: int) -> list[tuple[str, int, float, float]]:
    """(bucket, markets, average YES mid, YES win rate) at a decision time: the 'calibration atlas'."""
    rows: dict[int, list[tuple[float, bool]]] = {}
    for m in markets:
        t = m.ref - hours * 3600
        if not (period[0] <= m.day <= period[1]) or not (m.open_ts <= t < m.close_ts) or m.result not in ("yes", "no"):
            continue
        q = quote_at(m, t)
        if q is None:
            continue
        mid = (q.bid + q.ask) / 2
        i = next(i for i, (lo, hi) in enumerate(BUCKETS) if mid < hi or hi == 1.0)
        rows.setdefault(i, []).append((mid, m.result == "yes"))
    out = []
    for i, (lo, hi) in enumerate(BUCKETS):
        xs = rows.get(i, [])
        if xs:
            out.append((f"{100 * lo:.0f}–{100 * hi:.0f}¢", len(xs), sum(x for x, _ in xs) / len(xs),
                        sum(1 for _, w in xs if w) / len(xs)))
    return out


# ---------------------------------------------------------------- leads (rules fixed Nov 14, 2026, before running)
#
# Lead 1, Economics: the only group positive on the fit period, but with too few bets for the scan's rules.
#   Taker at 85–97¢ as before, but only on quotes with a spread of 3¢ or less, all three decision times pooled
#   (no choices left to make on fit), scored on May–Aug with the scan's four kill rules.
# Lead 2, the last hour: favorites at 65–90¢ (midpoint) seemed to win 7–10¢ more often than priced. Checked on
#   Oct–Apr only, because May–Aug has been looked at. It survives if, on quotes with a spread of 3¢ or less, a
#   taker buying the favorite at the ask made at least +3¢ per contract after fees, over at least 50 markets.
#   Otherwise it was the empty-book effect and is dropped. Surviving earns a fresh-data test, not money.

LEAD_SPREAD = 0.03
LEAD1_GROUP = "Economics"
LEAD2_RANGE = (0.65, 0.90)
LEAD2_MIN_MARKETS = 50
LEAD2_MIN_RETURN = 0.03


def lead1_bets(markets: list[Market]) -> list[Bet]:
    return [b for m in markets if m.group == LEAD1_GROUP for h in DECISION_HOURS
            for b in bets_for(m, h, max_spread=LEAD_SPREAD) if b.style == "taker"]


@dataclass(frozen=True)
class FavoriteRow:
    markets: int
    events: int
    mid: float | None  # average midpoint of the side bought
    ask: float | None  # average price paid, before fees
    won: float | None  # share that won
    ret: float | None  # profit per contract after fees, at the ask
    luck_pct: float | None


def favorite_bets(markets: list[Market], period: tuple[str, str], hours: int, lo: float, hi: float,
                  max_spread: float | None = None, traded: bool = False) -> list[tuple[Bet, float]]:
    """A taker buying whichever side's midpoint is in [lo, hi], at that side's ask. (bet, midpoint) pairs."""
    out = []
    for m in markets:
        t = m.ref - hours * 3600
        if not (period[0] <= m.day <= period[1]) or not (m.open_ts <= t < m.close_ts) or m.result not in ("yes", "no"):
            continue
        q = quote_at(m, t)
        if q is None or (max_spread is not None and q.ask - q.bid > max_spread + 1e-9) or (traded and q.volume <= 0):
            continue
        mid = (q.bid + q.ask) / 2
        if lo <= mid <= hi:
            side, ask, side_mid = "yes", q.ask, mid
        elif lo <= 1 - mid <= hi:
            side, ask, side_mid = "no", round(1 - q.bid, 4), 1 - mid
        else:
            continue
        out.append((Bet(m, hours, "taker", side, ask, ask + fee(m.taker_coef, ask), m.result == side, q.volume * ask),
                    side_mid))
    return out


def favorite_row(pairs: list[tuple[Bet, float]]) -> FavoriteRow:
    if not pairs:
        return FavoriteRow(0, 0, None, None, None, None, None)
    bets = [b for b, _ in pairs]
    n = len(pairs)
    return FavoriteRow(n, len({b.market.event for b in bets}), sum(mid for _, mid in pairs) / n,
                       sum(b.price for b in bets) / n, sum(b.won for b in bets) / n, ret(bets), luck(bets))


def lead2_survives(row: FavoriteRow) -> bool:
    return row.markets >= LEAD2_MIN_MARKETS and row.ret is not None and row.ret >= LEAD2_MIN_RETURN
