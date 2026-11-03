"""Polymarket US daily-high temperature markets (public data, no API key).

Each city/day is an event, e.g. temp-mdwhigh-2026-11-04, settled on the NWS Daily Climate
Report (docs: https://docs.polymarket.us/faqs/weather-faqs). Each bracket is its own
Yes/No market with a short title: "63 or below", "64 to 65", "72 or above".
"""

import json
import re
from dataclasses import dataclass
from datetime import date

import httpx

from .brackets import Bracket
from .http import Http

GATEWAY = "https://gateway.polymarket.us"
STANDARD_FEE = 0.0695  # taker theta, docs.polymarket.us/fees (Nov 2026); markets also report it


@dataclass(frozen=True)
class BracketMarket:
    slug: str
    bracket: Bracket
    yes_bid: float | None
    yes_ask: float | None
    fee_rate: float
    status: str
    yes_payout: float | None  # 1 or 0 once resolved

    @property
    def no_ask(self) -> float | None:
        return None if self.yes_bid is None else round(1 - self.yes_bid, 4)


def parse_bracket(title: str) -> Bracket | None:
    t = " ".join(title.lower().replace("°", "").replace("f", " ").split())
    if m := re.fullmatch(r"(-?\d+) or below", t):
        hi = int(m.group(1))
        return Bracket(f"≤{hi}", None, hi)
    if m := re.fullmatch(r"(-?\d+) or above", t):
        lo = int(m.group(1))
        return Bracket(f"≥{lo}", lo, None)
    if m := re.fullmatch(r"(-?\d+) to (-?\d+)", t):
        lo, hi = int(m.group(1)), int(m.group(2))
        return Bracket(f"{lo}-{hi}", lo, hi) if lo < hi else Bracket(str(lo), lo, lo)
    return None


def quote(q) -> float | None:
    return float(q["value"]) if q and q.get("value") not in (None, "") else None


def parse_event(event: dict) -> list[BracketMarket]:
    """Bracket markets in temperature order. Raises if the brackets don't tile the line exactly."""
    out = []
    for m in event.get("markets") or []:
        b = parse_bracket(m.get("titleShort") or "")
        if b is None:
            raise ValueError(f"can't read bracket from {m.get('titleShort')!r} ({m.get('slug')})")
        payout = None
        if m.get("status") == "MARKET_STATUS_RESOLVED":
            prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
            payout = prices[0] if prices and max(prices) >= 0.99 else None
        out.append(BracketMarket(
            slug=m["slug"],
            bracket=b,
            yes_bid=quote(m.get("bestBidQuote")),
            yes_ask=quote(m.get("bestAskQuote")),
            fee_rate=float(m.get("feeCoefficient") or STANDARD_FEE),
            status=m.get("status") or "",
            yes_payout=payout,
        ))
    out.sort(key=lambda bm: -1e9 if bm.bracket.low is None else bm.bracket.low)
    check_tiling([bm.bracket for bm in out])
    return out


def check_tiling(brackets: list[Bracket]) -> None:
    """Every whole degree must fall in exactly one bracket, or the probabilities won't add up."""
    if not brackets or brackets[0].low is not None or brackets[-1].high is not None:
        raise ValueError("brackets must start with 'or below' and end with 'or above'")
    for a, b in zip(brackets, brackets[1:]):
        if a.high is None or b.low is None or b.low != a.high + 1:
            raise ValueError(f"brackets {a.label} and {b.label} leave a gap or overlap")


def event_slug(prefix: str, day: date) -> str:
    return f"{prefix}-{day.isoformat()}"


def book_side(book: dict, side: str) -> list[tuple[float, float]]:
    """Asks for buying `side` ("yes" or "no"), cheapest first. Buying No = hitting Yes bids at 1 - bid."""
    data = book.get("marketData") or book
    if side == "yes":
        levels = [(float(l["px"]["value"]), float(l["qty"])) for l in data.get("offers") or []]
    else:
        levels = [(round(1 - float(l["px"]["value"]), 4), float(l["qty"])) for l in data.get("bids") or []]
    return sorted(levels)


class PolymarketUS:
    def __init__(self, http: Http, base_url: str = GATEWAY):
        self.http = http
        self.base = base_url.rstrip("/")

    def event(self, prefix: str, day: date) -> dict | None:
        try:
            return self.http.json(f"{self.base}/v1/events/slug/{event_slug(prefix, day)}").get("event")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None  # not listed (yet)
            raise

    def markets(self, prefix: str, day: date) -> list[BracketMarket] | None:
        ev = self.event(prefix, day)
        return parse_event(ev) if ev else None

    def book(self, slug: str) -> dict:
        return self.http.json(f"{self.base}/v1/markets/{slug}/book")

    def yes_payout(self, slug: str) -> float | None:
        m = self.http.json(f"{self.base}/v1/market/slug/{slug}").get("market") or {}
        if m.get("status") != "MARKET_STATUS_RESOLVED":
            return None
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        return prices[0] if prices and max(prices) >= 0.99 else None
