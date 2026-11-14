"""Shortlist #3, Step 1: Kalshi's daily gas-price markets, before any model. Kalshi data only; analysis only.

Rules fixed Nov 14, 2026, before fetching (PROJECT_NOTES.md, "Gas Step 1"). Every market here is "AAA's average
regular price on the value date is strictly greater than $X". Trading stops at 11:59 PM ET the evening before.

Part A, late-evening favorites: 1 hour before close, buy the side whose midpoint is 65–90¢ at its ask, spread ≤ 3¢.
Part B, national series only: is the near-the-money spread tight enough to trade, and how good is the market's
  own forecast (the strike where the YES midpoint crosses 50¢) against yesterday's value and a naive trend?
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

from ..replay.kalshi import Kalshi, parse_time
from . import analysis as an
from .fresh import candle_row, usable

SERIES = ("KXAAAGASD", "KXAAAGASDFL", "KXAAAGASDOH", "KXAAAGASDIL", "KXAAAGASDNJ", "KXAAAGASDCA",
          "KXAAAGASDNC", "KXAAAGASDMI", "KXAAAGASDTX")
NATIONAL = "KXAAAGASD"
PERIOD = ("2026-04-01", "2026-09-12")
PARTS = {"Apr–May": ("2026-04-01", "2026-05-31"), "Jun–Jul": ("2026-06-01", "2026-07-31"),
         "Aug–Sep 12": ("2026-08-01", "2026-09-12")}
MIN_POSITIVE_PARTS = 2
SEEN_HASH = 0.20  # events below this ticker hash could have been in the structural scan's sample
PER_EVENT = 6
FAVORITE = (0.65, 0.90)
B_HOURS = (14, 8, 2)  # about 10 AM, 4 PM and 10 PM ET
SPREAD_LIMIT = 0.03
NEAR_MONEY = (0.20, 0.80)
STEP2_RATIO = 0.80

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (ticker TEXT PRIMARY KEY, taker_coef REAL, done INTEGER DEFAULT 0, events INTEGER,
                                   skipped_seen INTEGER);
CREATE TABLE IF NOT EXISTS events (event TEXT PRIMARY KEY, series TEXT, day TEXT, value REAL, prior REAL, week_ago REAL);
CREATE TABLE IF NOT EXISTS markets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, event TEXT, strike REAL, ref REAL,
                                    open_ts REAL, close_ts REAL, result TEXT, candles_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (market INTEGER, end_ts INTEGER, bid REAL, ask REAL, low REAL, high REAL, volume REAL,
                                    PRIMARY KEY (market, end_ts)) WITHOUT ROWID;
"""


def utc_day(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def pick_markets(markets: list[dict], prior: float | None, n: int = PER_EVENT) -> list[dict]:
    """The n markets whose strike is closest to the previous day's value (known before trading opens)."""
    from .__main__ import hash_frac
    if prior is None:
        return sorted(markets, key=lambda m: hash_frac(m["ticker"]))[:n]
    return sorted(markets, key=lambda m: (abs(float(m["floor_strike"]) - prior), hash_frac(m["ticker"])))[:n]


class GasStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)


def fetch(kalshi: Kalshi, path: str, out=print) -> None:
    from .__main__ import hash_frac, reference

    st = GasStore(path)
    meta = {s["ticker"]: s for s in kalshi.series_list() if s["ticker"] in SERIES}
    done = {r[0] for r in st.db.execute("SELECT ticker FROM series WHERE done = 1")}
    for ser in SERIES:
        if ser in done or ser not in meta:
            continue
        taker, _ = an.fee_coefs(meta[ser].get("fee_type"), meta[ser].get("fee_multiplier"))
        by_event: dict[str, list[dict]] = {}
        refs: dict[str, float] = {}
        values: dict[str, float] = {}
        for m in kalshi.archived_series_markets(ser):
            if not usable(m) or m.get("floor_strike") is None or m.get("strike_type") != "greater":
                continue
            ref, kind = reference(m, sports=False)
            if ref is None or kind != "close":
                continue
            day = utc_day(ref)
            by_event.setdefault(m["event_ticker"], []).append(m)
            refs[m["event_ticker"]] = ref
            if m.get("expiration_value") not in (None, ""):
                values[day] = float(str(m["expiration_value"]).replace(",", ""))
        kept = skipped = 0
        for event, ms in by_event.items():
            day = utc_day(refs[event])
            if not (PERIOD[0] <= day <= PERIOD[1]):
                continue
            if hash_frac(event) < SEEN_HASH:
                skipped += 1
                continue
            prior, week_ago = values.get(shift(day, -1)), values.get(shift(day, -8))
            st.db.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?)",
                          (event, ser, day, values.get(day), prior, week_ago))
            st.db.executemany(
                "INSERT OR IGNORE INTO markets (ticker, event, strike, ref, open_ts, close_ts, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(m["ticker"], event, float(m["floor_strike"]), refs[event], parse_time(m["open_time"]),
                  parse_time(m["close_time"]), m["result"]) for m in pick_markets(ms, prior)])
            kept += 1
        st.db.execute("INSERT OR REPLACE INTO series VALUES (?, ?, 1, ?, ?)", (ser, taker, kept, skipped))
        st.db.commit()
        out(f"  {ser:14} {kept:4} events kept, {skipped:3} left out as possibly seen  "
            f"[{kalshi.requests:,} requests, {kalshi.limited:,} rate-limited]")
    for attempt in (1, 2):
        todo = st.db.execute("SELECT m.*, e.series FROM markets m JOIN events e USING (event) WHERE candles_done = 0").fetchall()
        if not todo:
            break
        out(f"Candles: {len(todo):,} markets (one request each)" + (", retrying" if attempt == 2 else "") + ".")
        failed = 0
        for i, m in enumerate(todo, 1):
            start = max(m["ref"] - (max(B_HOURS) + 2) * 3600, m["open_ts"] or 0)
            try:
                raw = kalshi.candles(m["ticker"], m["series"], start, m["ref"], archived=True)
            except RuntimeError as exc:
                failed += 1
                out(f"  skipped {m['ticker']} for now ({exc})")
                continue
            st.db.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)",
                              [(m["id"], *candle_row(c)) for c in raw])
            st.db.execute("UPDATE markets SET candles_done = 1 WHERE id = ?", (m["id"],))
            if i % 200 == 0 or i == len(todo):
                st.db.commit()
                out(f"  {i:,} of {len(todo):,} markets [{kalshi.requests:,} requests, {kalshi.limited:,} rate-limited]")
        st.db.commit()
        if failed:
            out(f"  {failed} markets still to fetch" + ("; rerun gas-fetch to finish them" if attempt == 2 else ""))


# ---------------------------------------------------------------- analysis


def load(path: str) -> tuple[list[an.Market], dict[str, sqlite3.Row]]:
    st = GasStore(path)
    coef = {r["ticker"]: r["taker_coef"] for r in st.db.execute("SELECT * FROM series")}
    events = {r["event"]: r for r in st.db.execute("SELECT * FROM events")}
    candles: dict[int, list[an.Candle]] = {}
    for c in st.db.execute("SELECT * FROM candles ORDER BY market, end_ts"):
        candles.setdefault(c["market"], []).append(an.Candle(c["end_ts"], c["bid"], c["ask"], c["low"], c["high"], c["volume"]))
    out = []
    for r in st.db.execute("SELECT m.*, e.series FROM markets m JOIN events e USING (event) WHERE candles_done = 1"):
        out.append(an.Market(r["ticker"], r["event"], r["series"], r["ref"], r["open_ts"], r["close_ts"], r["result"],
                             coef.get(r["series"], an.TAKER_COEF), 0.0, 1.0, tuple(candles.get(r["id"], ())),
                             sports=False))
    return out, events


def strike_of(m: an.Market) -> float:
    return float(m.ticker.rsplit("-", 1)[1].lstrip("T"))


def implied_value(markets: list[an.Market], t: float) -> float | None:
    """The market's forecast: where the YES midpoint of "greater than strike" crosses 50¢, interpolated."""
    pts = []
    for m in markets:
        q = an.quote_at(m, t) if m.open_ts <= t < m.close_ts else None
        if q is not None:
            pts.append((strike_of(m), (q.bid + q.ask) / 2))
    pts.sort()
    for (s1, p1), (s2, p2) in zip(pts, pts[1:]):
        if p1 >= 0.5 >= p2 and p1 > p2:
            return s1 + (p1 - 0.5) / (p1 - p2) * (s2 - s1)
    return None


def part_b(markets: list[an.Market], events: dict[str, sqlite3.Row]) -> dict:
    by_event: dict[str, list[an.Market]] = {}
    for m in markets:
        if m.group == NATIONAL:
            by_event.setdefault(m.event, []).append(m)
    rows = {}
    for h in B_HOURS:
        market_err, flat_err, trend_err = [], [], []
        for event, ms in by_event.items():
            e = events[event]
            if e["value"] is None or e["prior"] is None:
                continue
            v = implied_value(ms, ms[0].ref - h * 3600)
            if v is None:
                continue
            market_err.append(abs(e["value"] - v) * 100)
            flat_err.append(abs(e["value"] - e["prior"]) * 100)
            if e["week_ago"] is not None:
                trend = e["prior"] + (e["prior"] - e["week_ago"]) / 7
                trend_err.append(abs(e["value"] - trend) * 100)
        med = lambda xs: statistics.median(xs) if xs else None
        rows[h] = {"events": len(market_err), "market": med(market_err), "flat": med(flat_err), "trend": med(trend_err)}
    spreads = []
    for ms in by_event.values():
        for m in ms:
            t = m.ref - 8 * 3600
            q = an.quote_at(m, t) if m.open_ts <= t < m.close_ts else None
            if q is not None and NEAR_MONEY[0] <= (q.bid + q.ask) / 2 <= NEAR_MONEY[1]:
                spreads.append(q.ask - q.bid)
    return {"rows": rows, "spread": statistics.median(spreads) if spreads else None, "spread_n": len(spreads)}


def report(path: str, out=print) -> None:
    from .fresh import report as score
    markets, events = load(path)
    fmt = lambda v: "–" if v is None else f"{v:.2f}¢"
    out(f"Gas Step 1 (rules fixed Nov 14, 2026, before fetching): {len(markets):,} daily gas markets from "
        f"{len({m.event for m in markets}):,} events, {PERIOD[0]} to {PERIOD[1]}")
    st = GasStore(path)
    for r in st.db.execute("SELECT * FROM series ORDER BY ticker"):
        out(f"  {r['ticker']:14} {r['events']:4} events kept, {r['skipped_seen']:3} left out as possibly seen")

    lo, hi = FAVORITE
    out(f"\nPart A: favorites at {100 * lo:.0f}–{100 * hi:.0f}¢ (midpoint) 1 h before close, bought at the ask, "
        f"spread ≤ {100 * SPREAD_LIMIT:.0f}¢. Candles end on the hour and trading stops at 11:59 PM ET, so the quote "
        "used is the 10 PM ET one, as in Lead 2")
    bets = [b for b, _ in an.favorite_bets(markets, PERIOD, 1, lo, hi, max_spread=SPREAD_LIMIT)]
    a_ok = score(bets, PERIOD, PARTS, MIN_POSITIVE_PARTS, lambda b: b.market.group, out)
    out("  Part A: " + ("passes every kill rule." if a_ok else "fails a kill rule; the late-evening gas lead is dropped."))

    b = part_b(markets, events)
    out(f"\nPart B: national series. The market's forecast is where the YES midpoint crosses 50¢")
    out(f"  {'hours before close':20} {'events':>6} {'market':>8} {'yesterday':>10} {'trend':>8}   (median absolute error)")
    for h, r in b["rows"].items():
        out(f"  {h:>2} h ({'10 AM' if h == 14 else '4 PM' if h == 8 else '10 PM'} ET)       {r['events']:6} "
            f"{fmt(r['market']):>8} {fmt(r['flat']):>10} {fmt(r['trend']):>8}")
    spread_ok = b["spread"] is not None and b["spread"] <= SPREAD_LIMIT + 1e-9
    out(f"  Spread check: median near-the-money spread at 8 h {fmt(None if b['spread'] is None else 100 * b['spread'])} "
        f"on {b['spread_n']} quotes -> {'PASS' if spread_ok else 'FAIL'} (needs ≤ {100 * SPREAD_LIMIT:.0f}¢)")
    r8 = b["rows"][8]
    go = (spread_ok and r8["market"] is not None and r8["trend"] is not None
          and r8["market"] >= STEP2_RATIO * r8["trend"])
    out(f"  Step 2 test: market error at 8 h {fmt(r8['market'])} vs {100 * STEP2_RATIO:.0f}% of the trend's "
        f"{fmt(None if r8['trend'] is None else STEP2_RATIO * r8['trend'])} -> "
        + ("GO: the market isn't clearly better than a naive trend, so a wholesale model has room."
           if go else "NO GO: the market already beats a naive trend by more than 20% (or can't be traded)."))
    out("\nNext: " + ("forward paper test of Part A" + (" and Step 2 (the wholesale model; needs the EIA key)." if go else ".")
                      if a_ok else "Step 2, the wholesale model (needs the EIA key)." if go
                      else "the gas idea is dropped."))
