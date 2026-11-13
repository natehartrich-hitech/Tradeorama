"""Kalshi's public market data (no API key needed): settled markets and their trade history.

Kalshi keeps a live tier and a historical archive. Markets that settled, and trades made, before the cutoff
from GET /historical/cutoff are served only by the /historical endpoints (docs.kalshi.com, "Historical Data";
in November 2026 the cutoff was about two months back). Responses use fixed-point strings: prices in
dollars ("0.4500") and contract counts with two decimals ("31.00").
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

# The official SDK uses external-api.kalshi.com; api.elections.kalshi.com is the older name for the same API.
HOSTS = ("https://external-api.kalshi.com/trade-api/v2", "https://api.elections.kalshi.com/trade-api/v2")
PAGE = 1000  # the maximum page size for markets and trades


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def dollars(d: dict, key: str, legacy_cents_key: str) -> float | None:
    value = d.get(key)
    if value not in (None, ""):
        return float(value)
    cents = d.get(legacy_cents_key)
    return None if cents is None else float(cents) / 100


@dataclass(frozen=True)
class KalshiTrade:
    trade_id: str
    ticker: str
    ts: float
    yes_price: float  # the NO price is 1 − this
    contracts: float
    taker_yes: bool  # True when the taker ended up holding YES (bought YES or sold NO)
    block: bool  # matched off the order book (RFQ / block proposal), so not available to anyone else


def parse_trade(t: dict) -> KalshiTrade | None:
    yes = dollars(t, "yes_price_dollars", "yes_price")
    count = t.get("count_fp")
    contracts = float(count) if count not in (None, "") else float(t.get("count") or 0)
    side = t.get("taker_outcome_side") or t.get("taker_side")
    if yes is None or side not in ("yes", "no") or contracts <= 0:
        return None
    return KalshiTrade(
        trade_id=t["trade_id"],
        ticker=t["ticker"],
        ts=parse_time(t["created_time"]),
        yes_price=yes,
        contracts=contracts,
        taker_yes=side == "yes",
        block=bool(t.get("is_block_trade")),
    )


class Kalshi:
    def __init__(self, client: httpx.Client | None = None, hosts=HOSTS, min_interval: float = 0.1,
                 sleep=time.sleep, clock=time.monotonic):
        self.http = client or httpx.Client(timeout=30)
        self.hosts = list(hosts)
        # Unauthenticated reads are rate limited, and from the cloud the limit seemed shared with other traffic:
        # in Nov 2026 even 1 request a second saw some 429s, while a steady ~3 a second got ~2 through. So the
        # pace eases off a little after a 429 (to at most 0.6 s apart), retries soon, and recovers quickly.
        self.floor = self.min_interval = min_interval
        self.sleep, self.clock = sleep, clock
        self._last = -math.inf
        self.requests = 0
        self.limited = 0  # 429 answers

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(10):
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            try:
                r = self.http.get(self.hosts[0] + path, params=params)
            except httpx.TransportError as exc:
                if len(self.hosts) > 1:
                    self.hosts.pop(0)  # try the other host name
                    continue
                raise RuntimeError(f"can't reach Kalshi ({type(exc).__name__})") from None
            self.requests += 1
            if r.status_code == 429:
                self.limited += 1
                self.min_interval = min(max(self.min_interval * 1.15, 0.05), max(0.6, self.floor))
                wait = r.headers.get("retry-after")
                self.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 0.5 * (attempt + 1))
                continue
            if r.status_code >= 500:
                self.sleep(min(2 ** attempt, 30))
                continue
            self.min_interval = max(self.floor, self.min_interval * 0.95)
            if r.status_code != 200:
                raise RuntimeError(f"Kalshi {path} returned {r.status_code}: {r.text[:200]}")
            return r.json()
        raise RuntimeError(f"Kalshi {path} kept failing (rate limited or server errors); try again later")

    def _pages(self, path: str, key: str, params: dict, limit: int = PAGE):
        cursor = None
        while True:
            page = dict(params, limit=limit)
            if cursor:
                page["cursor"] = cursor
            d = self._get(path, page)
            items = d.get(key) or []
            yield from items
            cursor = d.get("cursor")
            if not cursor or not items:
                return

    def cutoff(self) -> dict[str, float]:
        """Archive cutoffs as Unix times: market_settled_ts, trades_created_ts, ..."""
        return {k: parse_time(v) for k, v in self._get("/historical/cutoff").items() if isinstance(v, str)}

    def series(self, ticker: str) -> dict:
        return self._get(f"/series/{ticker}").get("series") or {}

    def series_list(self) -> list[dict]:
        """Every series (about 16,000 in Nov 2026, in one response), with all-time volume."""
        return self._get("/series", {"include_volume": "true"}).get("series") or []

    def settled_events(self, series_ticker: str, min_close_ts: float):
        """Settled events of a series, newest first. The archive's events come back without their markets."""
        params = {"series_ticker": series_ticker, "status": "settled", "min_close_ts": int(min_close_ts)}
        yield from self._pages("/events", "events", params, limit=200)

    def event_markets(self, event_ticker: str, live_too: bool = True) -> list[dict]:
        found = list(self._pages("/historical/markets", "markets", {"event_ticker": event_ticker}))
        if found or not live_too:
            return found
        return list(self._pages("/markets", "markets", {"event_ticker": event_ticker}))

    def archived_series_markets(self, series_ticker: str):
        """Every archived market of a series, 1,000 per page (the archive has no time filter)."""
        yield from self._pages("/historical/markets", "markets", {"series_ticker": series_ticker})

    def candles(self, ticker: str, series_ticker: str, start_ts: float, end_ts: float, archived: bool) -> list[dict]:
        """Hourly candles (bid, ask and trade prices, volume) ending between start_ts and end_ts."""
        params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": 60}
        path = (f"/historical/markets/{ticker}/candlesticks" if archived
                else f"/series/{series_ticker}/markets/{ticker}/candlesticks")
        return self._get(path, params).get("candlesticks") or []

    def settled_markets(self, series_ticker: str, min_close_ts: float, max_close_ts: float,
                        settled_cutoff: float) -> list[dict]:
        """Every settled market of a series that closed in the range, from the archive and the live tier."""
        found: dict[str, dict] = {}
        if min_close_ts < settled_cutoff:
            # The archive has no time filter, so this lists the whole series (about 3,000 NBA markets a season).
            for m in self._pages("/historical/markets", "markets", {"series_ticker": series_ticker}):
                found[m["ticker"]] = m
        if max_close_ts >= settled_cutoff:
            params = {"series_ticker": series_ticker, "status": "settled",
                      "min_close_ts": int(min_close_ts), "max_close_ts": int(max_close_ts)}
            for m in self._pages("/markets", "markets", params):
                found.setdefault(m["ticker"], m)
        return list(found.values())

    def trades(self, ticker: str, min_ts: float, max_ts: float, trades_cutoff: float) -> list[KalshiTrade]:
        """Trades of one market with min_ts <= time < max_ts."""
        params = {"ticker": ticker, "min_ts": int(min_ts), "max_ts": int(math.ceil(max_ts))}
        raw = []
        if min_ts < trades_cutoff:
            raw += self._pages("/historical/trades", "trades", params)
        if max_ts >= trades_cutoff:
            raw += self._pages("/markets/trades", "trades", params)
        out: dict[str, KalshiTrade] = {}
        for t in raw:
            trade = parse_trade(t)
            if trade and min_ts <= trade.ts < max_ts:
                out[trade.trade_id] = trade
        return sorted(out.values(), key=lambda t: t.ts)
