"""Bracket definitions and the rounding rule that maps a continuous high to a reported whole °F."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Bracket:
    label: str
    low: int | None  # lowest reported °F in the bracket, None = open below
    high: int | None  # highest reported °F in the bracket, None = open above

    @property
    def lower_f(self) -> float:
        """Continuous lower bound. NWS reports whole degrees, rounding .5 up, so 46 covers [45.5, 46.5)."""
        return -math.inf if self.low is None else self.low - 0.5

    @property
    def upper_f(self) -> float:
        return math.inf if self.high is None else self.high + 0.5

    def contains(self, reported_f: int) -> bool:
        return (self.low is None or reported_f >= self.low) and (self.high is None or reported_f <= self.high)


def parse_edges(text: str | list[int]) -> list[int]:
    edges = [int(x) for x in (text.split(",") if isinstance(text, str) else text) if str(x).strip() != ""]
    if not edges:
        raise ValueError("give at least one bracket edge")
    if any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError(f"bracket edges must increase: {edges}")
    return edges


def make_brackets(edges: list[int]) -> list[Bracket]:
    """Upper edges, inclusive. [45, 47, 49] -> ≤45, 46-47, 48-49, ≥50."""
    out = [Bracket(f"≤{edges[0]}", None, edges[0])]
    for lo, hi in zip(edges, edges[1:]):
        low = lo + 1
        out.append(Bracket(f"{low}" if low == hi else f"{low}-{hi}", low, hi))
    out.append(Bracket(f"≥{edges[-1] + 1}", edges[-1] + 1, None))
    return out


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)
