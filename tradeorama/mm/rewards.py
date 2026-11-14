"""Kalshi Liquidity Incentive Program scoring, as published on its help page (Nov 2026). Pure functions.

Per snapshot and per side (YES bids, NO bids):
- The side counts only if its resting size reaches the period's target size. If either side falls short, the
  whole snapshot is excluded, for everyone.
- Reference price: walking down from the best bid, the first level where cumulative size reaches 1/5 of target.
- The orders that help reach the target size are scored as size x multiplier. The multiplier is 1 at or above the
  reference price and discount ** (ticks below it) under it. Scores are normalized within the side.
- A participant's snapshot score is its YES share plus its NO share, so all participants together score 2.

Over a period, reward = pool x (sum of your snapshot scores) / (2 x all snapshots, counted or not). So each second
of a period is worth pool / duration, split by (YES share + NO share) / 2, and excluded seconds pay nobody.
"""

from __future__ import annotations

from dataclasses import dataclass

TICK = 0.01


@dataclass(frozen=True)
class Program:
    start: float
    end: float
    reward: float  # dollars for the whole period
    target: float  # contracts
    discount: float  # 0..1

    def rate(self) -> float:
        """Dollars per second for the whole market."""
        return self.reward / max(self.end - self.start, 1.0)


def side_share(levels: list[tuple[float, float]], target: float, discount: float,
               ours: float) -> tuple[bool, float]:
    """(side reaches the target size, our share of the side's score) when we add `ours` contracts at the best bid.

    levels: (price, size) bids for this side, any order. Whole levels count at the target-size boundary, which
    makes everyone else's score a little bigger: conservative for us."""
    book = sorted(((p, s) for p, s in levels if s > 0), reverse=True)
    if ours > 0:
        if book:
            best_price, best_size = book[0]
            book[0] = (best_price, best_size + ours)
        else:
            return False, 0.0  # no bids at all: we don't quote an empty side
    depth = sum(s for _, s in book)
    if depth + 1e-9 < target or not book:
        return False, 0.0
    cum, ref = 0.0, book[-1][0]
    for p, s in book:
        cum += s
        if cum + 1e-9 >= target / 5:
            ref = p
            break
    total, cum = 0.0, 0.0
    for p, s in book:
        ticks = max(0, round((ref - p) / TICK))
        total += s * (discount ** ticks if ticks else 1.0)
        cum += s
        if cum + 1e-9 >= target:
            break
    mine = ours  # our order sits at the best bid, which is at or above the reference price
    return True, (mine / total if total > 0 else 0.0)


def snapshot_value(yes: list[tuple[float, float]], no: list[tuple[float, float]], programs: list[Program],
                   ts: float, seconds: float, ours_yes: float, ours_no: float) -> tuple[float, bool]:
    """Dollars our quote earns over `seconds` from this snapshot, and whether the snapshot counted.

    Each active program has its own target size and discount, so each is scored on its own."""
    earned, counted = 0.0, False
    for prog in programs:
        if not (prog.start <= ts < prog.end):
            continue
        ok_y, share_y = side_share(yes, prog.target, prog.discount, ours_yes)
        ok_n, share_n = side_share(no, prog.target, prog.discount, ours_no)
        if ok_y and ok_n:
            counted = True
            earned += prog.rate() * seconds * (share_y + share_n) / 2
    return earned, counted
