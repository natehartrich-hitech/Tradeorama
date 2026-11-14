"""Collector for the market-making test: reads Kalshi's public data only and places no orders.

Every minute: one batch request for the order books of up to 100 selected markets. Each minute it also reads
the trade tape of about a sixth of them, so each market's trades are read about every 6 minutes. Every hour: the
active liquidity programs, keeping the selection near 100 markets. Every 6 hours: each market's status and result.
It stops by itself 5 days after the first start (kept in mm.db), so restarts don't extend the test.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..heartbeat import Heartbeat
from ..replay.kalshi import Kalshi, parse_time, parse_trade
from .store import Store

TARGET_MARKETS = 100
MAX_PER_SERIES = 10
BOOK_EVERY_SEC = 60
TRADE_ROUNDS = 6  # each market's trades are read once every this many book rounds
PROGRAMS_EVERY_SEC = 3600
STATUS_EVERY_SEC = 6 * 3600
STOP_FILE = "mm.stop"
# Freight-adjacent markets are left out (the owner works in freight). Matched on series ticker and title.
FREIGHT = re.compile(r"suez|panama|hormuz|babelmandeb|bab el|mandeb|malacca|bosporus|strait|canal|shipping|"
                     r"\bship|tanker|container|port\b|ports\b|freight|truck|rail|cargo|logistic", re.I)
SKIP_CATEGORIES = {"Sports", "Exotics"}


def hash_frac(text: str) -> float:
    return int(hashlib.sha1(text.encode()).hexdigest()[:12], 16) / 16 ** 12


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def eligible(series: dict | None) -> bool:
    if series is None:
        return False
    if series.get("category") in SKIP_CATEGORIES:
        return False
    return not FREIGHT.search(f"{series.get('ticker', '')} {series.get('title', '')}")


def maker_coef(series: dict) -> float:
    m = float(series.get("fee_multiplier") or 1)
    return 0.0175 * m if series.get("fee_type") == "quadratic_with_maker_fees" else 0.0


def parse_levels(levels) -> list[list[float]]:
    return [[float(p), float(s)] for p, s in (levels or []) if float(s) > 0]


class Collector:
    def __init__(self, kalshi: Kalshi, store: Store, days: float = 5, clock=time.time, sleep=time.sleep,
                 heartbeat: Heartbeat | None = None, out=print, stop_file: str = STOP_FILE):
        self.k, self.st, self.clock, self.sleep, self.out = kalshi, store, clock, sleep, out
        self.hb = heartbeat or Heartbeat(None)
        self.stop_file = stop_file
        now = clock()
        if store.get("started") is None:
            store.put("started", now)
            store.put("ends", now + days * 86400)
        self.ends = store.get("ends")
        self.series: dict[str, dict] = {}
        self.last_programs = self.last_status = 0.0
        self.round = 0
        self.last_book: dict[str, str] = {}  # market -> last stored book, to store unchanged books as "="

    # ------------------------------------------------------------ selection

    def refresh_programs(self) -> None:
        if not self.series:
            self.series = {s["ticker"]: s for s in self.k.series_list()}
        now = self.clock()
        # One page of up to 10,000 (the maximum; smaller limits silently cap the list, with no next page).
        progs = list(self.k._pages("/incentive_programs", "incentive_programs",
                                   {"status": "active", "type": "liquidity"}, limit=10000))
        rows, by_market = [], {}
        for p in progs:
            market = p.get("market_ticker")
            series = p.get("series_ticker") or (market or "").split("-")[0]
            if not market or not p.get("start_date") or not p.get("end_date"):
                continue
            rows.append((p["id"], market, series, parse_time(p["start_date"]), parse_time(p["end_date"]),
                         (p.get("period_reward") or 0) / 10000, float(p.get("target_size_fp") or 0),
                         (p.get("discount_factor_bps") or 10000) / 10000))
            by_market.setdefault(market, series)
        live = {r[1] for r in rows if r[3] <= now < r[4]}  # markets with a program running now
        for m in self.st.selected():
            if m["ticker"] not in live:
                self.st.drop(m["ticker"])
        chosen = {m["ticker"] for m in self.st.selected()}
        per_series: dict[str, int] = {}
        for m in self.st.selected():
            per_series[m["series"]] = per_series.get(m["series"], 0) + 1
        candidates = sorted((m for m in by_market if m in live and m not in chosen), key=hash_frac)
        for m in candidates:
            if len(chosen) >= TARGET_MARKETS:
                break
            s = self.series.get(by_market[m])
            if not eligible(s) or per_series.get(by_market[m], 0) >= MAX_PER_SERIES:
                continue
            self.st.select(m, by_market[m], s.get("category"), maker_coef(s), now)
            chosen.add(m)
            per_series[by_market[m]] = per_series.get(by_market[m], 0) + 1
        followed = {m["ticker"] for m in self.st.all_markets()}
        self.st.save_programs([r for r in rows if r[1] in followed])  # only the markets this test follows
        self.st.db.commit()
        self.last_programs = now
        self.out(f"{utc(now)}  programs: {len(progs):,} active; following {len(chosen)} markets "
                 f"from {len(per_series)} series")

    def refresh_status(self) -> None:
        tickers = [m["ticker"] for m in self.st.all_markets()]
        for i in range(0, len(tickers), 100):
            d = self.k._get("/markets", {"tickers": ",".join(tickers[i:i + 100]), "limit": 1000})
            for m in d.get("markets") or []:
                self.st.set_status(m["ticker"], m.get("status"), m.get("result") or None)
        self.st.db.commit()
        self.last_status = self.clock()

    # ------------------------------------------------------------ data

    def read_books(self) -> int:
        tickers = [m["ticker"] for m in self.st.selected()]
        n = 0
        for i in range(0, len(tickers), 100):
            batch = tickers[i:i + 100]
            d = self.k._get("/markets/orderbooks", [("tickers", t) for t in batch])
            now = self.clock()
            for ob in d.get("orderbooks") or []:
                book = ob.get("orderbook_fp") or {}
                yes, no = parse_levels(book.get("yes_dollars")), parse_levels(book.get("no_dollars"))
                key = repr((yes, no))
                if self.last_book.get(ob["ticker"]) == key:
                    self.st.save_book(ob["ticker"], now, "=", "=")  # same as the previous snapshot
                else:
                    self.st.save_book(ob["ticker"], now, yes, no)
                    self.last_book[ob["ticker"]] = key
                n += 1
        self.st.db.commit()
        return n

    def read_trades(self) -> int:
        sel = self.st.selected()
        if not sel:
            return 0
        share = math.ceil(len(sel) / TRADE_ROUNDS)
        start = (self.round * share) % len(sel)
        n = 0
        for m in (sel + sel)[start:start + share]:
            since = m["trades_since"] or self.clock()
            now = self.clock()
            raw = list(self.k._pages("/markets/trades", "trades", {"ticker": m["ticker"], "min_ts": int(since) - 1}))
            rows = []
            for t in raw:
                tr = parse_trade(t)
                if tr and not tr.block:
                    rows.append((tr.ts, tr.trade_id, tr.yes_price, tr.contracts, tr.taker_yes))
            self.st.save_trades(m["ticker"], rows, now)
            n += len(rows)
        self.st.db.commit()
        return n

    # ------------------------------------------------------------ loop

    def stopped(self) -> bool:
        return Path(self.stop_file).exists()

    def run(self) -> str:
        self.out(f"Market-making test: paper only, no orders. Runs until {utc(self.ends)}. "
                 f"Stop any time with Ctrl+C or a file named {self.stop_file}; it resumes where it stopped.")
        self.hb.beat(force=True)
        while self.clock() < self.ends:
            if self.stopped():
                self.out("Stop file found; stopping. Run again to resume.")
                return "stopped"
            t0 = self.clock()
            try:
                if t0 - self.last_programs >= PROGRAMS_EVERY_SEC:
                    self.refresh_programs()
                if t0 - self.last_status >= STATUS_EVERY_SEC:
                    self.refresh_status()
                books = self.read_books()
                trades = self.read_trades()
                self.round += 1
                if self.round % 30 == 1:
                    c = self.st.counts()
                    self.out(f"{utc(t0)}  round {self.round}: {books} books, {trades} new trades; "
                             f"stored {c['books']:,} books and {c['trades']:,} trades so far "
                             f"[{self.k.requests:,} requests, {self.k.limited:,} rate-limited]")
            except RuntimeError as exc:
                self.out(f"{utc(t0)}  skipped a round ({exc}); trying again next minute")
            self.hb.beat()
            self.sleep(max(1.0, BOOK_EVERY_SEC - (self.clock() - t0)))
        self.refresh_status()
        self.out(f"Done: the test window ended at {utc(self.ends)}. Next: python -m tradeorama.mm report")
        self.hb.beat(force=True)
        return "done"


def heartbeat_from_env() -> Heartbeat:
    return Heartbeat(os.environ.get("MM_HEARTBEAT_URL"))
