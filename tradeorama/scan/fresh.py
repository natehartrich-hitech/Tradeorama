"""Fresh-data tests of the structural scan's two leads. Rules fixed Nov 14, 2026, before any of this data was fetched
(PROJECT_NOTES.md, "Fresh-data tests of the two leads"). Analysis only; no orders.

Lead 2, the last hour: every non-sports market whose reference time falls between Sep 14 and Nov 12, 2026
  (after the archive cutoff, so none of it was in the scan), from the scan's series. One hour before the
  reference time, a taker buys the side whose midpoint is 65–90¢, at its ask, on quotes with a spread of 3¢ or
  less. These markets are in Kalshi's live tier, where one batch request returns candles for 100 markets.
Lead 1, economic releases: every Economics market from the scan's top 25 Economics series whose reference time
  falls between Jan 1, 2023 and Sep 30, 2025 (before the scan's period). A taker buys at an 85–97¢ ask, spread
  3¢ or less, at 24, 6 and 1 h before, pooled. These are archived, so it's one request per market.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..replay.kalshi import Kalshi, parse_time
from . import analysis as an
from .store import Store

LEAD2_PERIOD = ("2026-09-14", "2026-11-12")
LEAD2_HALVES = {"Sep 14–Oct 13": ("2026-09-14", "2026-10-13"), "Oct 14–Nov 12": ("2026-10-14", "2026-11-12")}
LEAD2_MAX_PER_SERIES = 5000
LEAD1_PERIOD = ("2023-01-01", "2025-09-30")
LEAD1_YEARS = {"2023": ("2023-01-01", "2023-12-31"), "2024": ("2024-01-01", "2024-12-31"),
               "2025": ("2025-01-01", "2025-09-30")}
LEAD1_MAX_PER_EVENT = 8
LEAD1_MIN_POSITIVE_YEARS = 2
# Their markets open at most about an hour before their time, so there is never a quote an hour before.
SHORT_LIVED = {"fifteen_min", "hourly"}
SPORTS = "Sports"


def ts(day: str) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


def _num(d: dict, *keys) -> float | None:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return float(v)
    return None


def candle_row(c: dict) -> tuple:
    """(end, bid, ask, low, high, volume) from either candle format: the archive's ("close", "volume") or the
    live tier's ("close_dollars", "volume_fp")."""
    bid, ask, price = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
    return (int(c["end_period_ts"]), _num(bid, "close", "close_dollars"), _num(ask, "close", "close_dollars"),
            _num(price, "low", "low_dollars"), _num(price, "high", "high_dollars"),
            _num(c, "volume", "volume_fp") or 0.0)


def batches(markets: list[tuple[int, str, float]], size: int = 100, span_hours: float = 48) -> list[list[tuple]]:
    """Group (id, ticker, ref) so each batch request covers at most `size` markets within `span_hours`."""
    out, cur = [], []
    for m in sorted(markets, key=lambda m: m[2]):
        if cur and (len(cur) >= size or m[2] - cur[0][2] > span_hours * 3600):
            out.append(cur)
            cur = []
        cur.append(m)
    return out + ([cur] if cur else [])


def usable(m: dict) -> bool:
    return (m.get("market_type", "binary") == "binary" and not m.get("mve_collection_ticker")
            and m.get("result") in ("yes", "no"))


def save_series_list(kalshi: Kalshi, store: Store) -> dict[str, dict]:
    rows = kalshi.series_list()
    store.save_series([(s["ticker"], s.get("category"), s.get("fee_type"), s.get("fee_multiplier"),
                        float(s.get("volume_fp") or s.get("volume") or 0)) for s in rows])
    return {s["ticker"]: s for s in rows}


def save_market_rows(store: Store, series: str, kept: list[tuple[dict, float, str]], weight: float) -> None:
    by_event: dict[str, list] = {}
    for m, ref, kind in kept:
        by_event.setdefault(m["event_ticker"], []).append((m, ref, kind))
    for event, ms in by_event.items():
        store.save_event((event, series, None, None, None, 1.0))
        store.save_markets(event, [
            (m["ticker"], ref, kind, parse_time(m["open_time"]) if m.get("open_time") else None,
             parse_time(m["close_time"]) if m.get("close_time") else None,
             parse_time(m["settlement_ts"]) if m.get("settlement_ts") else None, m["result"], weight)
            for m, ref, kind in ms])


# ---------------------------------------------------------------- fetch


def fetch_lead2(kalshi: Kalshi, store: Store, out=print, per_category: int = 25) -> None:
    from .__main__ import hash_frac, pick_series, reference

    meta = save_series_list(kalshi, store)
    chosen = [s for s in pick_series(store.series_rows(), per_category, 0)
              if s["category"] != SPORTS and (meta.get(s["ticker"], {}).get("frequency") not in SHORT_LIVED)]
    todo = [s for s in chosen if s["status"] is None]
    out(f"Lead 2: {len(chosen)} non-sports series (hourly and 15-minute series left out), {len(todo)} still to list.")
    lo, hi = ts(LEAD2_PERIOD[0]), ts(LEAD2_PERIOD[1]) + 86400
    for s in todo:
        params = {"series_ticker": s["ticker"], "status": "settled", "min_close_ts": int(lo - 86400),
                  "max_close_ts": int(hi + 2 * 86400)}
        kept = []
        for m in kalshi._pages("/markets", "markets", params):
            if not usable(m):
                continue
            ref, kind = reference(m, sports=False)
            opened = parse_time(m["open_time"]) if m.get("open_time") else None
            if ref is None or not (lo <= ref < hi) or opened is None or opened >= ref - 3600:
                continue
            kept.append((m, ref, kind))
        weight = 1.0
        if len(kept) > LEAD2_MAX_PER_SERIES:
            weight = len(kept) / LEAD2_MAX_PER_SERIES
            kept = sorted(kept, key=lambda x: hash_frac(x[0]["ticker"]))[:LEAD2_MAX_PER_SERIES]
        save_market_rows(store, s["ticker"], kept, weight)
        store.set_series_status(s["ticker"], "sampled", None, len(kept))
        out(f"  {s['category']:22} {s['ticker']:28} {len(kept):6,} markets  [{kalshi.requests:,} requests, "
            f"{kalshi.limited:,} rate-limited]")
    todo_m = [(m["id"], m["ticker"], m["ref_ts"]) for m in store.markets_todo()]
    groups = batches(todo_m)
    out(f"Candles: {len(todo_m):,} markets in {len(groups):,} batch requests.")
    for i, g in enumerate(groups, 1):
        try:
            d = kalshi._get("/markets/candlesticks", {
                "market_tickers": ",".join(t for _, t, _ in g), "start_ts": int(g[0][2] - 7200),
                "end_ts": int(g[-1][2] - 3600), "period_interval": 60})
        except RuntimeError as exc:  # rate limited for too long: a rerun picks these markets up again
            out(f"  skipped a batch of {len(g)} markets for now ({exc}); rerun fresh-fetch to finish it")
            continue
        found = {x.get("market_ticker"): x.get("candlesticks") or [] for x in d.get("markets") or []}
        for mid, ticker, _ in g:
            store.save_candles(mid, [candle_row(c) for c in found.get(ticker, [])])
        store.commit()
        if i % 50 == 0 or i == len(groups):
            out(f"  {i:,} of {len(groups):,} batches [{kalshi.requests:,} requests, {kalshi.limited:,} rate-limited]")


def fetch_lead1(kalshi: Kalshi, store: Store, out=print, per_category: int = 25) -> None:
    from .__main__ import hash_frac, pick_series, reference

    save_series_list(kalshi, store)
    chosen = [s for s in pick_series(store.series_rows(), per_category, 0) if s["category"] == "Economics"]
    todo = [s for s in chosen if s["status"] is None]
    out(f"Lead 1: {len(chosen)} Economics series, {len(todo)} still to list.")
    lo, hi = ts(LEAD1_PERIOD[0]), ts(LEAD1_PERIOD[1]) + 86400
    for s in todo:
        by_event: dict[str, list] = {}
        for m in kalshi.archived_series_markets(s["ticker"]):
            if not usable(m):
                continue
            ref, kind = reference(m, sports=False)
            if ref is not None and lo <= ref < hi:
                by_event.setdefault(m["event_ticker"], []).append((m, ref, kind))
        n = 0
        for event, ms in by_event.items():
            ms = sorted(ms, key=lambda x: hash_frac(x[0]["ticker"]))
            weight = max(1.0, len(ms) / LEAD1_MAX_PER_EVENT)
            save_market_rows(store, s["ticker"], ms[:LEAD1_MAX_PER_EVENT], weight)
            n += min(len(ms), LEAD1_MAX_PER_EVENT)
        store.set_series_status(s["ticker"], "sampled", None, len(by_event))
        out(f"  {s['ticker']:28} {len(by_event):5,} events, {n:6,} markets  [{kalshi.requests:,} requests, "
            f"{kalshi.limited:,} rate-limited]")
    for attempt in (1, 2):  # a second pass retries markets that kept failing during the first
        todo_m = store.markets_todo()
        if not todo_m:
            break
        out(f"Candles: {len(todo_m):,} markets (archived: one request each)" + (", retrying" if attempt == 2 else "") + ".")
        failed = 0
        for i, m in enumerate(todo_m, 1):
            start = max(m["ref_ts"] - (max(an.DECISION_HOURS) + 1) * 3600, m["open_ts"] or 0)
            end = min(m["ref_ts"] + 3600, (m["close_ts"] or m["ref_ts"]) + 3600)
            try:
                raw = kalshi.candles(m["ticker"], m["series"], start, end, archived=True) if end > start else []
            except RuntimeError as exc:  # rate limited for too long: leave it for the next pass or a rerun
                failed += 1
                out(f"  skipped {m['ticker']} for now ({exc})")
                continue
            store.save_candles(m["id"], [candle_row(c) for c in raw])
            if i % 200 == 0 or i == len(todo_m):
                store.commit()
                out(f"  {i:,} of {len(todo_m):,} markets [{kalshi.requests:,} requests, {kalshi.limited:,} rate-limited]")
        store.commit()
        if failed:
            out(f"  {failed} markets still to fetch" + ("; rerun fresh-fetch to finish them" if attempt == 2 else ""))


# ---------------------------------------------------------------- report


def rules(s: an.Summary, parts: dict[str, float | None], need_positive: int) -> list[an.Rule]:
    positive = sum(1 for r in parts.values() if r is not None and r > 0)
    return [
        an.Rule(f"Return after fees at least {100 * an.MIN_RETURN:+.0f}¢ per contract",
                "–" if s.ret is None else f"{100 * s.ret:+.2f}¢", s.ret is not None and s.ret >= an.MIN_RETURN),
        an.Rule(f"Luck check under {an.MAX_LUCK_PCT:.0f}%", "–" if s.luck_pct is None else f"{s.luck_pct:.1f}%",
                s.luck_pct is not None and s.luck_pct < an.MAX_LUCK_PCT),
        an.Rule(f"Positive in at least {need_positive} of {len(parts)} parts", f"{positive}", positive >= need_positive),
        an.Rule(f"At least ${an.MIN_WEEKLY_DOLLARS:,.0f} a week traded at qualifying prices",
                f"${s.weekly_dollars:,.0f}", s.weekly_dollars >= an.MIN_WEEKLY_DOLLARS),
    ]


def report(bets: list[an.Bet], period: tuple[str, str], parts: dict[str, tuple[str, str]], need_positive: int,
           group_of, out) -> bool:
    fmt = lambda v: "–" if v is None else f"{100 * v:+.2f}¢"
    s = an.summarize(bets, period)
    part_ret = {name: an.ret([b for b in bets if lo <= b.market.day <= hi]) for name, (lo, hi) in parts.items()}
    if s.bets:
        out(f"  {s.bets:,} bets on {s.events:,} events over {s.days} days; won {100 * s.win_rate:.1f}% at an average "
            f"price of {100 * s.avg_price:.1f}¢; profit {fmt(s.ret)} per contract; worst day ${s.worst_day:,.0f}")
        out("  By part: " + ", ".join(f"{k}: {fmt(v)}" for k, v in part_ret.items()))
        groups: dict[str, list[an.Bet]] = {}
        for b in bets:
            groups.setdefault(group_of(b), []).append(b)
        out("  By group (for information; the verdict is on the total):")
        for g, bs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:10]:
            out(f"    {g[:28]:28} {len(bs):6,} bets {fmt(an.ret(bs)):>9}")
    rs = rules(s, part_ret, need_positive)
    for r in rs:
        out(f"  {'PASS' if r.passed else 'FAIL'}  {r.text}: {r.value}")
    return all(r.passed for r in rs)


def report_lead2(store: Store, out=print) -> bool:
    from .__main__ import load

    markets = [m for m in load(store) if not m.sports]
    lo, hi = an.LEAD2_RANGE
    out(f"Lead 2 on fresh data ({LEAD2_PERIOD[0]} to {LEAD2_PERIOD[1]}): favorites at {100 * lo:.0f}–{100 * hi:.0f}¢ "
        f"(midpoint) 1 h before their time, bought at the ask, spread ≤ {100 * an.LEAD_SPREAD:.0f}¢")
    out(f"  {len(markets):,} markets with candles from {len({m.event for m in markets}):,} events")
    bets = [b for b, _ in an.favorite_bets(markets, LEAD2_PERIOD, 1, lo, hi, max_spread=an.LEAD_SPREAD)]
    ok = report(bets, LEAD2_PERIOD, LEAD2_HALVES, len(LEAD2_HALVES), lambda b: b.market.group, out)
    out("Verdict: " + ("passes every kill rule. Next: live paper trading against the real order book, not money."
                       if ok else "fails a kill rule. Lead 2 is dropped."))
    return ok


def report_lead1(store: Store, out=print) -> bool:
    from .__main__ import load

    markets = [m for m in load(store) if m.group == "Economics"]
    out(f"Lead 1 on earlier data ({LEAD1_PERIOD[0]} to {LEAD1_PERIOD[1]}): Economics, taker at "
        f"{100 * an.LOW:.0f}–{100 * an.HIGH:.0f}¢, spread ≤ {100 * an.LEAD_SPREAD:.0f}¢, 24/6/1 h pooled")
    out(f"  {len(markets):,} markets with candles from {len({m.event for m in markets}):,} events")
    bets = [b for m in markets if LEAD1_PERIOD[0] <= m.day <= LEAD1_PERIOD[1] for h in an.DECISION_HOURS
            for b in an.bets_for(m, h, max_spread=an.LEAD_SPREAD) if b.style == "taker"]
    ok = report(bets, LEAD1_PERIOD, LEAD1_YEARS, LEAD1_MIN_POSITIVE_YEARS,
                lambda b: b.market.ticker.split("-")[0], out)
    out("Verdict: " + ("passes every kill rule. Next: a forward paper test on economic-release markets, not money."
                       if ok else "fails a kill rule. Lead 1 is dropped."))
    return ok
