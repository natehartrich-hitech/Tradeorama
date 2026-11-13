"""Command line: python -m tradeorama.scan {fetch,report}

fetch   sample settled Kalshi markets and download their hourly candles (public data, free)
report  apply the pre-set hypotheses and kill rules (see analysis.py and PROJECT_NOTES.md)
"""

import argparse
import hashlib
import logging
import sys
import time
from datetime import datetime, timezone

from ..replay.kalshi import Kalshi, parse_time
from . import analysis as an
from .store import Store

DEFAULT_DB = "scan.db"
SPORTS, SKIP_CATEGORIES = "Sports", {"Exotics"}  # Exotics are combos (multi-leg parlays)
MIN_SERIES_VOLUME = 1_000_000  # contracts, all time
PERIOD_START = datetime.fromisoformat(an.FIT[0]).replace(tzinfo=timezone.utc).timestamp()
PERIOD_END = datetime.fromisoformat(an.HOLDOUT[1]).replace(tzinfo=timezone.utc).timestamp() + 86400
LISTED_MONTHS = 14  # settled events listed from Oct 2025 run to about now (Nov 2026)


def hash_frac(text: str) -> float:
    """A fixed pseudo-random number in [0, 1) per ticker, so the sample doesn't depend on outcomes or order."""
    return int(hashlib.sha1(text.encode()).hexdigest()[:12], 16) / 16 ** 12


def opt_time(value) -> float | None:
    return parse_time(value) if value else None


def pick_series(rows, per_category: int, sports: int) -> list:
    by_cat: dict[str, list] = {}
    for r in rows:
        if r["category"] in SKIP_CATEGORIES or (r["volume"] or 0) < MIN_SERIES_VOLUME:
            continue
        by_cat.setdefault(r["category"], []).append(r)
    out = []
    for cat, rs in sorted(by_cat.items()):
        rs.sort(key=lambda r: -(r["volume"] or 0))
        out += rs[: sports if cat == SPORTS else per_category]
    return out


def reference(m: dict, sports: bool) -> tuple[float | None, str]:
    """A market's reference time, fixed when it was created, and which kind it is.

    Kalshi's `early_close_condition` says how a market can close. Empty, or "... regardless of any data releases
    or events" (weather), means trading stops at a fixed time, so the close is the schedule. Otherwise ("closes
    early if the event occurs", sports "after a winner is declared") the actual close depends on what happened,
    and the scheduled `expected_expiration_time` is used instead. It isn't moved when a market closes early: the
    Fed chair nominee market closed in March 2026 with its expected expiration still in January 2029."""
    cond = (m.get("early_close_condition") or "").lower()
    if not sports and (not cond or "regardless" in cond):
        return opt_time(m.get("close_time")), "close"
    return opt_time(m.get("expected_expiration_time")), "expected"


def sample_series(kalshi: Kalshi, store: Store, s, args, out) -> None:
    """List a series' settled events, sample them, and record their sampled markets in the period."""
    sports = s["category"] == SPORTS
    listed = list(kalshi.settled_events(s["ticker"], PERIOD_START - 2 * 86400))
    if not listed:
        store.set_series_status(s["ticker"], "no settled events in the period", 0, 0)
        return
    # Events don't say when they happen (strike_date only exists since mid-Aug 2026), so sample over the whole
    # listing (Oct 2025 to now) and drop markets whose reference time is outside the period afterwards.
    frac = min(1.0, args.per_month * LISTED_MONTHS / len(listed))
    sampled = sorted((e for e in listed if hash_frac(e["event_ticker"]) < frac), key=lambda e: hash_frac(e["event_ticker"]))
    sampled = sampled or [min(listed, key=lambda e: hash_frac(e["event_ticker"]))]
    # Markets for the sampled events: one request per event, or, when the whole series' archive is fewer pages
    # than that (daily series, sports), the series' archive pages. Events settled after the archive cutoff (Sept 13,
    # 2026) are after the period anyway, so the live tier isn't asked.
    first = kalshi.event_markets(sampled[0]["event_ticker"], live_too=False)
    pages = len(listed) * max(1, len(first)) / 1000
    by_event: dict[str, list[dict]] | None = None
    if pages < 0.8 * len(sampled):
        by_event = {}
        for m in kalshi.archived_series_markets(s["ticker"]):
            by_event.setdefault(m["event_ticker"], []).append(m)
    checked, in_period = False, 0
    for i, e in enumerate(sampled):
        if by_event is not None:
            raw = by_event.get(e["event_ticker"], [])
        else:
            raw = first if i == 0 else kalshi.event_markets(e["event_ticker"], live_too=False)
        markets = []
        for m in raw:
            if m.get("market_type", "binary") != "binary" or m.get("mve_collection_ticker") or m.get("result") not in ("yes", "no"):
                continue
            ref, kind = reference(m, sports)
            if ref is not None and PERIOD_START <= ref < PERIOD_END:
                markets.append((m, ref, kind))
        if markets and not checked:
            checked = True
            if max(ref - (opt_time(m.get("open_time")) or ref) for m, ref, _ in markets) < 3600 + 600:
                store.set_series_status(s["ticker"], "markets open less than an hour before their time", len(listed), 0)
                return
        in_period += bool(markets)
        markets.sort(key=lambda x: hash_frac(x[0]["ticker"]))
        weight = (1 / frac) * max(1.0, len(markets) / args.markets_per_event)
        store.save_event((e["event_ticker"], s["ticker"], opt_time(e.get("strike_date")), e.get("fee_type_override"),
                          e.get("fee_multiplier_override"), 1 / frac))
        store.save_markets(e["event_ticker"], [
            (m["ticker"], ref, kind, opt_time(m.get("open_time")), opt_time(m.get("close_time")),
             opt_time(m.get("settlement_ts")), m["result"], weight)
            for m, ref, kind in markets[: args.markets_per_event]])
    store.set_series_status(s["ticker"], "sampled", len(listed), in_period)
    out(f"  {s['category']:22} {s['ticker']:28} {len(listed):6,} events listed, {len(sampled):4} sampled, "
        f"{in_period:4} in the period  [{kalshi.requests:,} requests, {kalshi.limited:,} rate-limited]")


def cmd_fetch(args, kalshi: Kalshi | None = None, out=print) -> None:
    store = Store(args.db)
    kalshi = kalshi or Kalshi(min_interval=args.interval)
    start = time.time()
    cutoff = kalshi.cutoff().get("market_settled_ts", 0)
    if not store.series_rows():
        rows = kalshi.series_list()
        store.save_series([(s["ticker"], s.get("category"), s.get("fee_type"), s.get("fee_multiplier"),
                            float(s.get("volume_fp") or s.get("volume") or 0)) for s in rows])
    chosen = pick_series(store.series_rows(), args.per_category, args.sports)
    todo = [s for s in chosen if s["status"] is None]
    out(f"Series: {len(chosen)} chosen (top {args.per_category} per category by volume, {args.sports} for sports), "
        f"{len(todo)} still to sample.")
    for s in todo:
        sample_series(kalshi, store, s, args, out)
    markets = store.markets_todo()
    out(f"Candles: {len(markets):,} markets to download (one request each).")
    for i, m in enumerate(markets, 1):
        lo = max(m["ref_ts"] - (max(an.DECISION_HOURS) + 1) * 3600, m["open_ts"] or 0)
        hi = min(m["ref_ts"] + 3600, (m["close_ts"] or m["ref_ts"]) + 3600)
        rows = []
        if hi > lo:
            archived = (m["settled_ts"] or 0) < cutoff
            for c in kalshi.candles(m["ticker"], m["series"], lo, hi, archived):
                bid, ask, price = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
                num = lambda v: None if v in (None, "") else float(v)
                rows.append((int(c["end_period_ts"]), num(bid.get("close")), num(ask.get("close")),
                             num(price.get("low")), num(price.get("high")), num(c.get("volume")) or 0.0))
        store.save_candles(m["id"], rows)
        if i % 200 == 0 or i == len(markets):
            store.commit()
            out(f"  {i:,} of {len(markets):,} markets ({time.time() - start:,.0f} s, {kalshi.requests:,} requests, "
                f"{kalshi.limited:,} rate-limited)")
    store.commit()
    c = store.counts()
    out(f"Done: {c['events']:,} events, {c['markets_with_candles']:,} markets with candles, {c['candles']:,} candles, "
        f"{kalshi.requests:,} requests this run in {time.time() - start:,.0f} s.")


# ---------------------------------------------------------------- report


def load(store: Store) -> list[an.Market]:
    out = []
    for r in store.all_markets():
        fee_type = r["ev_fee_type"] or r["s_fee_type"]
        mult = r["ev_fee_multiplier"] if r["ev_fee_type"] else r["s_fee_multiplier"]
        taker, maker = an.fee_coefs(fee_type, mult)
        candles = tuple(an.Candle(c["end_ts"], c["bid"], c["ask"], c["low"], c["high"], c["volume"])
                        for c in store.candles_of(r["id"]))
        group = r["series"] if r["category"] == SPORTS else r["category"]
        out.append(an.Market(r["ticker"], r["event_ticker"], group, r["ref_ts"], r["open_ts"] or 0,
                             r["close_ts"] or r["ref_ts"], r["result"], taker, maker, r["weight"] or 1.0, candles,
                             sports=r["category"] == SPORTS))
    return out


def fmt_c(v) -> str:
    return "–" if v is None else f"{100 * v:+.2f}¢"


def report_universe(title: str, markets: list[an.Market], out) -> bool:
    out(f"\n=== {title} ===")
    bets = an.all_bets(markets)
    fit = [b for b in bets if an.in_period(b, an.FIT)]
    hold = [b for b in bets if an.in_period(b, an.HOLDOUT)]
    out(f"Markets: {len(markets):,} sampled ({len({m.event for m in markets}):,} events, "
        f"{len({m.group for m in markets})} groups). Bets at 85–97¢: {len(fit):,} in fit (Oct–Apr), "
        f"{len(hold):,} in holdout (May–Aug).")
    if not bets:
        out("Nothing to test.")
        return False

    out("\nCalibration: how often YES won, by its midpoint price (all sampled markets with a quote)")
    for h in an.DECISION_HOURS:
        f = {b: (n, mid, w) for b, n, mid, w in an.calibration(markets, an.FIT, h)}
        g = {b: (n, mid, w) for b, n, mid, w in an.calibration(markets, an.HOLDOUT, h)}
        if not f and not g:
            continue
        out(f"  {h} h before    {'fit: n':>9} {'mid':>6} {'won':>6}   {'holdout: n':>10} {'mid':>6} {'won':>6}")
        for lo, hi in an.BUCKETS:
            key = f"{100 * lo:.0f}–{100 * hi:.0f}¢"
            a, b = f.get(key), g.get(key)
            if a or b:
                fa = f"{a[0]:9,} {100 * a[1]:5.1f}¢ {100 * a[2]:5.1f}%" if a else f"{'':9} {'':6} {'':6}"
                fb = f"{b[0]:10,} {100 * b[1]:5.1f}¢ {100 * b[2]:5.1f}%" if b else ""
                out(f"  {key:13} {fa}   {fb}")

    out("\nFit period only (Oct–Apr): profit per contract after fees, by group and decision time")
    cells: dict[tuple[str, int, str], list[an.Bet]] = {}
    for b in fit:
        cells.setdefault((b.market.group, b.hours, b.style), []).append(b)
    groups = sorted({g for g, _, _ in cells})
    out(f"  {'group':28} {'hours':>5} {'taker n':>8} {'taker':>9} {'maker n':>8} {'maker':>9}")
    for g in groups:
        for h in an.DECISION_HOURS:
            t, m = cells.get((g, h, "taker"), []), cells.get((g, h, "maker"), [])
            if len(t) + len(m) >= 50:
                out(f"  {g[:28]:28} {h:5} {len(t):8,} {fmt_c(an.ret(t)):>9} {len(m):8,} {fmt_c(an.ret(m)):>9}")

    passed_any = False
    out("\nHypotheses (choices made on the fit period, scored once on May–Aug)")
    for hyp in an.choose(fit):
        out(f"\n{hyp.name}: {hyp.note}")
        if not hyp.picks:
            out("  FAIL  not testable: nothing to score")
            continue
        s = an.summarize(hyp.select(hold), an.HOLDOUT, an.HOLDOUT_MONTHS)
        if s.bets:
            out(f"  {s.bets:,} bets on {s.events:,} events over {s.days} days; won {100 * s.win_rate:.1f}% at an "
                f"average price of {100 * s.avg_price:.1f}¢; profit {fmt_c(s.ret)} per contract; worst day "
                f"${s.worst_day:,.0f} at $100 a bet")
            out("  By month: " + ", ".join(f"{m[5:]}: {fmt_c(r)}" for m, r in s.months.items()))
        rules = an.kill_rules(s)
        for r in rules:
            out(f"  {'PASS' if r.passed else 'FAIL'}  {r.text}: {r.value}")
        passed_any |= all(r.passed for r in rules)
    out("\nVerdict: " + ("at least one hypothesis passes every kill rule. With three tries, treat a narrow pass "
                         "as weak; next is live paper trading against the real order book."
                         if passed_any else "every hypothesis fails a kill rule. By the rules set in advance, "
                         "the bias isn't tradable here after fees."))
    return passed_any


def cmd_report(args, out=print) -> None:
    store = Store(args.db)
    markets = load(store)
    c = store.counts()
    out(f"Kalshi structural scan: contracts priced {100 * an.LOW:.0f}–{100 * an.HIGH:.0f}¢ at "
        f"{', '.join(str(h) for h in an.DECISION_HOURS)} h before their scheduled time, ${an.STAKE:.0f} a bet.")
    out(f"Cache: {c['events']:,} events, {c['markets_with_candles']:,} of {c['markets']:,} markets with candles.")
    skipped = {}
    for r in store.series_rows():
        if r["status"] and r["status"] != "sampled":
            skipped[r["status"]] = skipped.get(r["status"], 0) + 1
    for why, n in sorted(skipped.items()):
        out(f"Series left out: {n} ({why})")
    kinds = dict(store.db.execute("SELECT ref_kind, COUNT(*) FROM markets WHERE candles_done = 1 GROUP BY ref_kind"))
    out(f"Reference times: {kinds.get('close', 0):,} markets from a fixed close, {kinds.get('expected', 0):,} from "
        f"the scheduled expiration (markets that close early when their event happens, and sports)")
    sports = [m for m in markets if m.sports]
    non_sports = [m for m in markets if not m.sports]
    report_universe("Non-sports categories (the main verdict)", non_sports, out)
    report_universe("Sports, reported separately (Illinois' rules on sports contracts are unsettled)", sports, out)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m tradeorama.scan", description="Kalshi favorite-longshot scan")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="sample settled markets and download their hourly candles (free)")
    f.add_argument("--per-category", type=int, default=25, help="series per category, by volume (default 25)")
    f.add_argument("--sports", type=int, default=30, help="sports series, by volume (default 30)")
    f.add_argument("--per-month", type=float, default=3, help="events sampled per series per month (default 3)")
    f.add_argument("--markets-per-event", type=int, default=3, help="markets sampled per event (default 3)")
    f.add_argument("--interval", type=float, default=0.3,
                   help="shortest gap between Kalshi requests; it slows down by itself when rate limited (default 0.3)")
    f.add_argument("--db", default=DEFAULT_DB)
    f.set_defaults(fn=cmd_fetch)
    r = sub.add_parser("report", help="apply the pre-set hypotheses and kill rules")
    r.add_argument("--db", default=DEFAULT_DB)
    r.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
