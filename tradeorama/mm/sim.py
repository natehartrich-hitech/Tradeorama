"""Simulated two-sided quoting in one market: rewards, fills, inventory and P&L. Pure functions.

At each book snapshot the quote joins the best bid on each side with `size` contracts: YES at the best YES bid,
NO at the best NO bid. It keeps its place in the queue while the best bid holds, and goes to the back of the queue
at the new price when it moves. Trades between snapshots fill it in order: first the size that was ahead of it
at its price, then it. A trade through its price means that whole level was taken, so it fills.

Kalshi's tape: a taker who ends up holding YES took resting NO bids at (1 - yes_price); one who ends up holding
NO took resting YES bids at yes_price.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from . import rewards as rw

MAX_GAP_SEC = 300  # a longer gap between snapshots isn't credited (the collector was down or slow)
MARKOUT_SEC = 3600


@dataclass(frozen=True)
class Book:
    ts: float
    yes: tuple[tuple[float, float], ...]  # (price, size) bids
    no: tuple[tuple[float, float], ...]

    def best(self, side: str) -> tuple[float, float] | None:
        levels = self.yes if side == "yes" else self.no
        return max(levels) if levels else None

    def mid_yes(self) -> float | None:
        by, bn = self.best("yes"), self.best("no")
        if by is None or bn is None:
            return None
        return (by[0] + (1 - bn[0])) / 2  # YES ask = 1 - best NO bid


@dataclass(frozen=True)
class Trade:
    ts: float
    yes_price: float
    count: float
    taker_yes: bool


@dataclass(frozen=True)
class Fill:
    ts: float
    side: str
    price: float
    qty: float
    fee: float


@dataclass
class Result:
    rewards: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    fees: float = 0.0
    position: dict = field(default_factory=lambda: {"yes": 0.0, "no": 0.0})
    cost: dict = field(default_factory=lambda: {"yes": 0.0, "no": 0.0})
    capital_seconds: float = 0.0  # committed dollars x seconds
    seconds: float = 0.0  # time credited
    counted_seconds: float = 0.0  # of which snapshots counted for rewards
    by_day: dict = field(default_factory=dict)  # day index -> rewards

    def avg_capital(self) -> float:
        return self.capital_seconds / self.seconds if self.seconds else 0.0


def maker_fee(price: float, qty: float, coef: float) -> float:
    return coef * qty * price * (1 - price)


def simulate(books: list[Book], trades: list[Trade], programs: list[rw.Program], size: float, cap: float,
             maker_coef: float = 0.0, day_of=lambda ts: 0) -> Result:
    res = Result()
    trades = sorted(trades, key=lambda t: t.ts)
    times = [t.ts for t in trades]
    quote = {"yes": None, "no": None}  # side -> [price, remaining, queue_ahead]
    for k, book in enumerate(books):
        nxt = books[k + 1].ts if k + 1 < len(books) else None
        gap = (nxt - book.ts) if nxt is not None else 0.0
        credited = min(gap, MAX_GAP_SEC)
        for side in ("yes", "no"):
            best = book.best(side)
            if best is None or res.position[side] >= cap - 1e-9:
                quote[side] = None
                continue
            q = quote[side]
            if q is not None and abs(q[0] - best[0]) < 1e-9:
                q[1] = size  # top back up; the queue position stays
                q[2] = min(q[2], best[1])
            else:
                quote[side] = [best[0], size, best[1]]  # join at the back of the best level
        ours = {s: (quote[s][1] if quote[s] else 0.0) for s in ("yes", "no")}
        if credited > 0:
            earned, counted = rw.snapshot_value(list(book.yes), list(book.no), programs, book.ts, credited,
                                                ours["yes"], ours["no"])
            res.rewards += earned
            res.by_day[day_of(book.ts)] = res.by_day.get(day_of(book.ts), 0.0) + earned
            res.seconds += credited
            res.counted_seconds += credited if counted else 0.0
            resting = sum(quote[s][0] * quote[s][1] for s in ("yes", "no") if quote[s])
            res.capital_seconds += (resting + res.cost["yes"] + res.cost["no"]) * credited
        if nxt is None:
            break
        lo, hi = bisect.bisect_right(times, book.ts), bisect.bisect_right(times, nxt)
        for t in trades[lo:hi]:
            side = "no" if t.taker_yes else "yes"  # which resting bids the taker hit
            q = quote[side]
            if q is None or q[1] <= 0:
                continue
            hit_price = t.yes_price if side == "yes" else round(1 - t.yes_price, 6)
            if hit_price > q[0] + 1e-9:
                continue  # traded above our bid: someone better was hit
            if hit_price < q[0] - 1e-9:
                qty = q[1]  # went through our price: our level was cleared
                q[2] = 0.0
            else:
                take = t.count
                ahead = min(q[2], take)
                q[2] -= ahead
                qty = min(q[1], take - ahead)
            if qty <= 0:
                continue
            fee = maker_fee(q[0], qty, maker_coef)
            q[1] -= qty
            res.position[side] += qty
            res.cost[side] += qty * q[0] + fee
            res.fees += fee
            res.fills.append(Fill(t.ts, side, q[0], qty, fee))
            if res.position[side] >= cap - 1e-9:
                quote[side] = None
    return res


def value(res: Result, books: list[Book], result: str | None) -> float:
    """What the inventory is worth: at settlement if known, else at the last midpoint."""
    if result in ("yes", "no"):
        return res.position[result] * 1.0
    mid = next((b.mid_yes() for b in reversed(books) if b.mid_yes() is not None), None)
    if mid is None:
        return 0.0
    return res.position["yes"] * mid + res.position["no"] * (1 - mid)


def markouts(res: Result, books: list[Book], after: float = MARKOUT_SEC) -> list[float]:
    """Per filled contract: the side's midpoint `after` seconds later minus the price paid (negative = picked off)."""
    ts = [b.ts for b in books]
    out = []
    for f in res.fills:
        i = bisect.bisect_left(ts, f.ts + after)
        if i >= len(books):
            continue
        mid = books[i].mid_yes()
        if mid is None:
            continue
        side_mid = mid if f.side == "yes" else 1 - mid
        out.extend([side_mid - f.price] * max(1, round(f.qty)))
    return out
