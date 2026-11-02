"""Command line: python -m tradeorama.strategy {scan,run,report,settle}"""

import argparse
import logging
import sys

from ..config import load_settings, load_strategy_settings
from ..trader import Trader
from .bot import STOP_FILE, Bot
from .ledger import Ledger
from .matching import SportsMarkets, candidates, market_start, match, why_unmatched
from .odds import PinnacleOdds


def build(live: bool = False) -> Bot:
    settings, strat = load_settings(), load_strategy_settings()
    return Bot(
        settings,
        strat,
        PinnacleOdds(strat.odds_api_key or ""),
        SportsMarkets(),
        Trader(settings),
        Ledger(strat.db_path),
        live=live,
    )


def cmd_scan(args) -> None:
    bot = build()
    decisions = bot.cycle(execute=False)
    if not decisions:
        print("No Polymarket markets matched any Pinnacle games.")
        return
    print(f"{'start (UTC)':16} {'outcome':40} {'fair':>6} {'ask':>6} {'edge':>7}  action")
    for d in sorted(decisions, key=lambda d: (d.leg.line.commence, d.leg.market_id)):
        ask = f"{d.best_ask:.3f}" if d.best_ask is not None else "–"
        edge = f"{100 * d.edge:+.1f}¢" if d.edge is not None else "–"
        what = f"BUY {d.size:.2f} for ${d.cost:.2f}" if d.action == "buy" else d.reason
        print(f"{d.leg.line.commence:%m-%d %H:%M}      {d.leg.label[:40]:40} {d.leg.fair:6.3f} {ask:>6} {edge:>7}  {what}")
    print(f"\nNothing was traded. Check every row above is the right game and outcome. Odds credits left: {bot.odds.credits_remaining}")


def cmd_run(args) -> None:
    bot = build(live=args.live)
    if args.live:
        print("LIVE MODE: real orders will be placed. Create a file named", STOP_FILE, "or press Ctrl+C to stop.")
    else:
        print("Paper mode: trades are simulated against the live order book and recorded, nothing is sent.")
    try:
        bot.run(args.interval, args.cycles, adaptive=not args.fixed)
    except KeyboardInterrupt:
        print("\nStopped.")


def fmt_cents(v) -> str:
    return "–" if v is None else f"{v:+.1f}¢"


def fmt_usd(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):.2f}"


def fmt_pct(v) -> str:
    return "–" if v is None else f"{v:+.1f}%"


def print_sport_table(ledger: Ledger, mode: str) -> None:
    rows = ledger.summary_by_sport(mode)
    total = ledger.summary(mode)
    print(f"{'sport':28} {'trades':>6} {'staked':>9} {'avg edge':>9} {'avg CLV':>8} {'(n)':>5} {'settled P&L':>12} {'ROI':>7}")
    for sport, r in [*rows.items(), ("ALL", total)]:
        print(
            f"{sport:28} {r['trades']:6d} {'$' + format(r['staked_usd'], '.2f'):>9} {fmt_cents(r['avg_edge_cents']):>9} "
            f"{fmt_cents(r['avg_clv_cents']):>8} {r['clv_trades']:5d} {fmt_usd(r['settled_pnl_usd']):>12} "
            f"{fmt_pct(r['roi_pct']):>7}"
        )


def cmd_report(args) -> None:
    ledger = Ledger(load_strategy_settings().db_path)
    for mode in ("paper", "live"):
        if not ledger.summary(mode)["trades"]:
            continue
        print(f"== {mode} ==")
        print_sport_table(ledger, mode)
        print()
    print("(n) is how many trades have a closing line yet. Judge a sport on CLV only after ~100 of them.")
    trades = ledger.trades(limit=args.limit)
    if trades:
        print(f"\nLast {len(trades)} trades:")
        for t in trades:
            pnl = fmt_usd(t["pnl"]) if t["pnl"] is not None else "open"
            clv = fmt_cents(100 * t["clv"]) if t["clv"] is not None else "–"
            print(
                f"  {t['mode']:5} {t['sport'][:20]:20} {t['label'][:36]:36} {t['size']:8.2f} sh ${t['cost']:7.2f}  "
                f"edge {100 * t['edge']:+.1f}¢  CLV {clv:>7}  {pnl}"
            )
    else:
        print("No trades yet.")


def cmd_diagnose(args) -> None:
    strat = load_strategy_settings()
    odds, sports = PinnacleOdds(strat.odds_api_key or ""), SportsMarkets()
    lines = odds.lines(args.sport)
    events = sports.events(args.sport)
    n_markets = sum(len(e.get("markets") or []) for e in events)
    with_start = sum(1 for e in events for m in e.get("markets") or [] if market_start(m, e))
    print(f"Pinnacle games: {len(lines)}")
    print(f"Polymarket events: {len(events)} ({n_markets} markets, {with_start} with a game start time)")
    for line in sorted(lines, key=lambda l: l.commence)[: args.games]:
        legs = match(line, events)
        print(f"\n{line.commence:%m-%d %H:%M} UTC  {line.away} at {line.home}: {len(legs)} outcomes matched")
        found = candidates(line, events)
        if not found:
            print("  no Polymarket market mentions either team")
        for m, event in found[: args.markets]:
            print(f"  - {(m.get('question') or '')[:70]}")
            print(f"      {why_unmatched(m, event, line)}")
        if len(found) > args.markets:
            print(f"  ... and {len(found) - args.markets} more markets mentioning these teams")
    if events and not any(candidates(l, events) for l in lines):
        print("\nSample of Polymarket events fetched:")
        for e in events[:10]:
            print(f"  - {e.get('title')}")
    print(f"\nOdds credits left: {odds.credits_remaining}")


def cmd_settle(args) -> None:
    print(f"Settled {build().settle()} trades.")


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # httpx logs every request URL at INFO, and Odds API URLs contain the API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m tradeorama.strategy", description="Pinnacle +EV sports bot for Polymarket")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="show matched markets and current edges; trades nothing").set_defaults(fn=cmd_scan)
    r = sub.add_parser("run", help="trade on a loop (paper by default)")
    r.add_argument("--live", action="store_true", help="place real orders (also needs DRY_RUN=false)")
    r.add_argument("--interval", type=float, default=300,
                   help="seconds between checks in the hours before a game (default 300)")
    r.add_argument("--fixed", action="store_true",
                   help="check every --interval seconds regardless of game times (uses more Odds API credits)")
    r.add_argument("--cycles", type=int, default=None, help="stop after this many cycles")
    r.set_defaults(fn=cmd_run)
    rep = sub.add_parser("report", help="results so far, including closing line value")
    rep.add_argument("--limit", type=int, default=20)
    rep.set_defaults(fn=cmd_report)
    sub.add_parser("settle", help="record payouts for finished games").set_defaults(fn=cmd_settle)
    d = sub.add_parser("diagnose", help="explain why a sport's games do or don't match Polymarket markets")
    d.add_argument("sport", help="an Odds API sport key, e.g. americanfootball_nfl")
    d.add_argument("--games", type=int, default=5, help="how many upcoming games to explain (default 5)")
    d.add_argument("--markets", type=int, default=6, help="related markets to show per game (default 6)")
    d.set_defaults(fn=cmd_diagnose)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
