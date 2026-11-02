"""Command line: python -m tradeorama.strategy {scan,run,report,settle}"""

import argparse
import logging
import sys

from ..config import load_settings, load_strategy_settings
from ..trader import Trader
from .bot import STOP_FILE, Bot
from .ledger import Ledger
from .matching import SportsMarkets
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


def cmd_report(args) -> None:
    ledger = Ledger(load_strategy_settings().db_path)
    for mode in ("paper", "live"):
        s = ledger.summary(mode)
        if not s["trades"]:
            continue
        print(f"== {mode} ==")
        for k, v in s.items():
            print(f"  {k:22} {v}")
    trades = ledger.trades(limit=args.limit)
    if trades:
        print(f"\nLast {len(trades)} trades:")
        for t in trades:
            clv = f"{100 * t['clv']:+.1f}¢" if t["clv"] is not None else "–"
            pnl = f"${t['pnl']:+.2f}" if t["pnl"] is not None else "open"
            print(f"  {t['mode']:5} {t['label'][:40]:40} {t['size']:8.2f} sh ${t['cost']:7.2f}  edge {100 * t['edge']:+.1f}¢  CLV {clv:>7}  {pnl}")
    else:
        print("No trades yet.")


def cmd_settle(args) -> None:
    print(f"Settled {build().settle()} trades.")


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
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
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
