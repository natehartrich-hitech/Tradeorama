"""Command line: python -m tradeorama.replay {fetch,report}

fetch   download and cache Kalshi games and trades (free) and Pinnacle snapshots (Odds API credits, asks first)
report  replay the cached data and apply the kill rules
"""

import argparse
import dataclasses
import logging
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

from ..strategy.odds import parse_events
from . import analysis as an
from .history import EVENTS_CALL_CREDITS, ODDS_CALL_CREDITS, OddsHistory, estimated_credits, snapshot_times
from .kalshi import Kalshi, parse_time
from .store import Store
from .teams import SERIES, event_date, match_event

DEFAULT_DB = "replay.db"
# A game's Kalshi markets close when it's settled, so they can't close sooner than this after a correct start
# time. The Odds API listed Minnesota at Memphis (Feb 2 2026) about 1½ hours late, which made in-game trades
# and Pinnacle's in-game odds look like pregame gaps.
MIN_GAME_HOURS = {"basketball_nba": 1.75, "americanfootball_nfl": 2.5}


class Budget(Exception):
    """Stop fetching from The Odds API; everything fetched so far stays cached."""


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def day_ts(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def fmt_cents(v) -> str:
    return "–" if v is None else f"{100 * v:+.1f}¢"


def fmt_usd(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def fmt_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


def series_for(args) -> str:
    series = args.series or SERIES.get(args.sport)
    if not series:
        raise ValueError(f"no Kalshi series known for {args.sport}; pass --series")
    return series


def in_range(event_ticker: str, first: date, last: date) -> bool:
    d = event_date(event_ticker)
    return d is not None and first <= d <= last


# ---------------------------------------------------------------- fetch


def cmd_fetch(args, kalshi: Kalshi | None = None, make_odds=None, ask=input, out=print) -> None:
    sport, series = args.sport, series_for(args)
    first, last = parse_day(args.start), parse_day(args.end)
    store = Store(args.db)
    kalshi = kalshi or Kalshi()

    cutoff = kalshi.cutoff()
    s = kalshi.series(series)
    if s.get("ticker"):
        store.save_series(s)
    markets = kalshi.settled_markets(series, day_ts(first) - 86400, day_ts(last) + 2 * 86400,
                                     cutoff.get("market_settled_ts", 0))
    for m in markets:
        if in_range(m["event_ticker"], first, last):
            store.save_market(series, m)
    store.commit()
    events = {e: rows for e, rows in store.events(series).items() if in_range(e, first, last)}
    game_days = sorted({event_date(e) for e in events})
    out(f"Kalshi: {plural(len(events), series + ' game')} on {plural(len(game_days), 'day')} from {first} to {last} "
        f"(fee multiplier {s.get('fee_multiplier', '?')}).")
    if not events:
        return

    est = estimated_credits(len(game_days), args.hours, args.step)
    out(f"The Odds API: this needs up to about {est:,} credits (less for anything already cached). It stops at "
        f"--max-credits {args.max_credits:,} and leaves at least --keep-credits {args.keep_credits:,} for the bot.")
    if not args.yes and ask("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Nothing was spent.")
        return
    odds = make_odds() if make_odds else OddsHistory(os.environ.get("ODDS_API_KEY") or "")

    def spend(credits: int) -> None:
        if odds.spent + credits > args.max_credits:
            raise Budget(f"this run reached --max-credits ({args.max_credits:,})")
        if odds.remaining is not None and odds.remaining - credits < args.keep_credits:
            raise Budget(f"only {odds.remaining:,.0f} credits left this month (--keep-credits {args.keep_credits:,})")

    try:
        # 1. The games and start times on each day (1 credit a day).
        pool: dict[str, dict] = {}
        for d in game_days:
            body = store.odds_events(sport, d.isoformat())
            if body is None:
                spend(EVENTS_CALL_CREDITS)
                body = odds.events(sport, day_ts(d) + 10 * 3600)  # 5 AM Eastern: the day's games are all upcoming
                store.save_odds_events(sport, d.isoformat(), body)
            for e in body.get("data") or []:
                pool[e["id"]] = {**e, "commence_ts": parse_time(e["commence_time"])}

        # 2. Match each Kalshi game to its Odds API game.
        problems = []
        for e, rows in sorted(events.items()):
            game, info = match_event(sport, e, [r["code"] for r in rows], list(pool.values()))
            if game is None:
                problems.append((e, info))
            else:
                store.save_game(sport, event_date(e).isoformat(), e, game, info)
        store.commit()
        out(f"Matched {len(events) - len(problems)} of {len(events)} Kalshi games to Odds API games.")
        for e, why in problems[:15]:
            out(f"  not matched: {e} ({why})")
        if len(problems) > 15:
            out(f"  ... and {len(problems) - 15} more")
        games = store.games(sport, first.isoformat(), last.isoformat())

        # 3. Kalshi trades before each start (free).
        for i, g in enumerate(games, 1):
            lo, hi = int(g["commence"] - args.hours * 3600), int(g["commence"])
            for m in store.markets_of(g["event_ticker"]):
                if not store.fetched(m["ticker"], lo, hi):
                    store.save_trades(m["ticker"], lo, hi,
                                      kalshi.trades(m["ticker"], lo, hi, cutoff.get("trades_created_ts", 0)))
            if i % 25 == 0 or i == len(games):
                out(f"Kalshi trades: {i} of {len(games)} games")

        # 4. Pinnacle snapshots (10 credits each).
        by_day: dict[str, list[float]] = {}
        for g in games:
            by_day.setdefault(g["day"], []).append(g["commence"])
        plan = [t for starts in by_day.values() for t in snapshot_times(starts, args.hours, args.step)
                if not store.has_snapshot(sport, t)]
        cost = ODDS_CALL_CREDITS * len(plan)
        out(f"Pinnacle snapshots: {len(plan)} to fetch, {cost:,} credits.")
        if odds.spent + cost > args.max_credits:
            raise Budget(f"that would pass --max-credits ({args.max_credits:,}); use a shorter date range or raise it")
        for i, t in enumerate(plan, 1):
            spend(ODDS_CALL_CREDITS)
            store.save_snapshot(sport, t, odds.odds(sport, t))
            if i % 50 == 0 or i == len(plan):
                out(f"  {i} of {len(plan)} snapshots ({odds.remaining:,.0f} credits left this month)"
                    if odds.remaining is not None else f"  {i} of {len(plan)} snapshots")
    except Budget as stop:
        out(f"Stopped: {stop}. Everything fetched so far is cached; rerun to continue.")
        return
    finally:
        out(f"Odds API credits used by this run: {odds.spent:,.0f}.")
    out(f"Done. Next: python -m tradeorama.replay report --sport {sport} --from {first} --to {last}")


# ---------------------------------------------------------------- report


def winner_of(markets, code_team: dict[str, str]) -> str | None:
    yes = [m for m in markets if m["result"] == "yes"]
    no = [m for m in markets if m["result"] == "no"]
    if len(yes) == 1 and len(no) == len(markets) - 1:
        return code_team.get(yes[0]["code"])
    return None  # voided, postponed or not settled


def start_looks_wrong(sport: str, commence: float, closes: list[float | None]) -> bool:
    closed = [c for c in closes if c is not None]
    return bool(closed) and max(closed) - commence < MIN_GAME_HOURS.get(sport, 1.5) * 3600


def load(store: Store, sport: str, first: str, last: str, hours: float):
    """Games, pregame trades, skipped block trades, and games left out because their start time looks wrong."""
    rows = store.games(sport, first, last)
    games: list[an.Game] = []
    trades: dict[str, list[an.Trade]] = {}
    blocks: dict[str, int] = {}
    wrong_start: list[tuple[str, float, float]] = []  # (event ticker, listed start, Kalshi close)
    if not rows:
        return games, trades, blocks, wrong_start
    lines: dict[str, dict[float, dict[str, float]]] = {}
    for ts, data in store.snapshots(sport, min(r["commence"] for r in rows) - hours * 3600 - 3600,
                                    max(r["commence"] for r in rows) + 1):
        for line in parse_events(data):
            lines.setdefault(line.event_id, {})[ts] = line.fair
    for r in rows:
        commence = r["commence"]
        snaps = tuple(an.Snapshot(ts, fair) for ts, fair in sorted(lines.get(r["odds_event_id"], {}).items())
                      if ts < commence)
        markets = store.markets_of(r["event_ticker"])
        if start_looks_wrong(sport, commence, [m["close_ts"] for m in markets]):
            wrong_start.append((r["event_ticker"], commence, max(m["close_ts"] for m in markets if m["close_ts"])))
            continue
        code_team = {r["home_code"]: r["home"], r["away_code"]: r["away"]}
        games.append(an.Game(r["event_ticker"], r["day"], commence, r["home"], r["away"], snaps,
                             winner_of(markets, code_team)))
        out, n_block = [], 0
        for m in markets:
            team = code_team.get(m["code"])
            if team is None:
                continue
            other = r["away"] if team == r["home"] else r["home"]
            for t in store.trades(m["ticker"], commence - hours * 3600, commence):
                if t["block"]:
                    n_block += 1
                    continue
                waived = m["fee_waiver_until"] is not None and t["ts"] < m["fee_waiver_until"]
                if t["taker_yes"]:
                    out.append(an.Trade(t["ts"], team, t["yes_price"], t["contracts"], waived))
                else:
                    out.append(an.Trade(t["ts"], other, round(1 - t["yes_price"], 6), t["contracts"], waived))
        trades[r["event_ticker"]] = sorted(out, key=lambda t: t.ts)
        blocks[r["event_ticker"]] = n_block
    return games, trades, blocks, wrong_start


def print_groups(title: str, groups: list[an.Group], out) -> None:
    out(f"  {title:24} {'windows':>8} {'avg CLV':>8} {'staked':>10} {'P&L':>10}")
    for g in groups:
        out(f"  {g.label:24} {g.windows:8d} {fmt_cents(g.clv):>8} {'$' + format(g.staked, ',.0f'):>10} "
            f"{fmt_usd(g.pnl):>10}")


def cmd_report(args, out=print) -> None:
    sport, series = args.sport, series_for(args)
    store = Store(args.db)
    multiplier = store.fee_multiplier(series)
    fee_coef = args.fee_coef if args.fee_coef is not None else 0.07 * (multiplier if multiplier is not None else 1)
    prm = an.Params(min_edge=args.min_edge, hours=args.hours, fee_coef=fee_coef, il_tax=args.il_tax,
                    stake=args.stake, tolerance=args.tolerance, max_divergence=args.max_divergence,
                    max_age_sec=(args.step + 5) * 60)
    games, trades, blocks, wrong_start = load(store, sport, args.start, args.end, args.hours)
    kalshi_games = sum(1 for e in store.events(series) if in_range(e, parse_day(args.start), parse_day(args.end)))
    rep = an.replay(games, trades, prm, blocks)

    out(f"Replay: Kalshi {series} trades vs Pinnacle's no-vig line, {args.start} to {args.end}")
    out(f"Buy when Pinnacle − (price + fees{' + tax' if prm.il_tax else ''}) ≥ {100 * prm.min_edge:.1f}¢. "
        f"Taker fee {prm.fee_coef:.4f} × p × (1 − p)"
        + (f", Illinois tax {100 * prm.il_tax:.2f}% of the price" if prm.il_tax else "")
        + f". Trades from {prm.hours:g} h before the start. Paper stake ${prm.stake:,.0f} per window.")
    out("")
    out("Coverage")
    out(f"  Games: {kalshi_games} on Kalshi, {len(games) + len(wrong_start)} matched to Pinnacle, {rep.games} with "
        f"snapshots and pregame trades, on {plural(rep.days, 'game day')}")
    if wrong_start:
        out(f"  Left out {plural(len(wrong_start), 'game')} whose Kalshi market closed too soon after the listed start "
            f"for a whole game (the start time is probably wrong):")
        for e, start, closed in wrong_start[:10]:
            out(f"    {e}: listed start {fmt_utc(start)} UTC, Kalshi closed {fmt_utc(closed)} UTC")
    out(f"  Pregame trades: {rep.trades:,} (${rep.dollars:,.0f}); block trades left out: {rep.skipped.get('block', 0):,}")
    if not rep.games:
        out("\nNothing to replay. Run fetch for this sport and date range first.")
        return

    out("How closely Kalshi trades track Pinnacle (dollar-weighted, against the latest snapshot)")
    med = an.weighted_median(rep.gaps.items())
    dist = an.weighted_median((abs(g), d) for g, d in rep.gaps.items())
    out(f"  Median price − Pinnacle: {fmt_cents(med)}; median distance: {fmt_cents(dist).lstrip('+')}")
    out("Trades left out of the gaps")
    for key, text in an.SKIP_REASONS.items():
        if key != "block" and rep.skipped.get(key):
            out(f"  {rep.skipped[key]:>9,}  {text}")

    w = rep.windows
    sides = {(x.game.key, x.team) for x in w}
    out("")
    out("Gaps")
    out(f"  Windows: {len(w):,} (one per team per snapshot interval), on {len(sides):,} game-sides "
        f"({len(sides) / rep.days:.2f} per game day)")
    if w:
        out(f"  Traded at qualifying prices: median ${statistics.median(x.dollars for x in w):,.0f} per window, "
            f"${sum(x.dollars for x in w):,.0f} in all")
        out(f"  Average edge at entry: {fmt_cents(sum(x.edge * x.bought(prm.stake) for x in w) / sum(x.bought(prm.stake) for x in w))}")
        clv = an.weighted_clv(w, prm.stake)
        rng = an.clv_range(w, prm.stake)
        positive = sum(1 for x in w if x.clv > 0) / len(w)
        out(f"  Average CLV (Pinnacle's close − cost with fees): {fmt_cents(clv)}"
            + (f" (95% range {fmt_cents(rng[0])} to {fmt_cents(rng[1])})" if rng else "")
            + f"; {100 * positive:.0f}% of windows beat the close")

        settled = [x for x in w if x.pnl(prm.stake) is not None]
        staked = sum(x.spend(prm.stake) for x in settled)
        pnl = sum(x.pnl(prm.stake) for x in settled)
        out("")
        out(f"Paper results at up to ${prm.stake:,.0f} per window")
        out(f"  {len(settled)} settled windows, staked ${staked:,.0f}, P&L {fmt_usd(pnl)}"
            + (f" (ROI {100 * pnl / staked:+.1f}%)" if staked else ""))
        luck = an.luck_check(w, prm.stake)
        if luck:
            out(f"  Is it luck? If Pinnacle's closing line was right: expected {fmt_usd(luck.close_expected)}.")
            out(f"  With no skill (prices paid were the true odds), a result at least this good happens about "
                f"{luck.chance_pct:.0f}% of the time" + (" -> unlikely to be luck" if luck.chance_pct < 5
                                                           else " -> can't rule out luck"))
        out("")
        out("Splits")
        print_groups("hours before the start", an.split(w, prm.stake, an.hours_bucket, ["under 1 h", "1–3 h", "3 h or more"]), out)
        print_groups("side", an.split(w, prm.stake, an.side_bucket, ["favorite", "underdog"]), out)
        print_groups("edge at entry", an.split(w, prm.stake, an.edge_bucket, ["3–5¢", "5–10¢", "10¢ or more"]), out)

        if not prm.il_tax:
            taxed = an.replay(games, trades, dataclasses.replace(prm, il_tax=0.0175), blocks)
            out("")
            out(f"If Illinois' 1.75% tax applies: {len(taxed.windows):,} windows, average CLV "
                f"{fmt_cents(an.weighted_clv(taxed.windows, prm.stake))}")

        out("")
        out("Largest gaps (check each is the right game and team; times UTC):")
        for x in sorted(w, key=lambda x: x.edge, reverse=True)[: args.top]:
            out(f"  {fmt_utc(x.first_ts)}  {x.game.away} at {x.game.home}: {x.team[:24]:24} "
                f"Pinnacle {x.fair:.3f}, paid {x.avg_price:.3f}, edge {fmt_cents(x.edge)}, "
                f"${x.dollars:,.0f} traded, close {x.close:.3f}, CLV {fmt_cents(x.clv)}")

    out("")
    out("Kill rules (set before looking)")
    rules = an.kill_rules(rep)
    for r in rules:
        out(f"  {'PASS' if r.passed else 'FAIL'}  {r.text}: {r.value}")
    if all(r.passed for r in rules):
        out("Verdict: passes the first test. Next, rerun on more months and the NFL; then paper trade on Kalshi.")
    else:
        out("Verdict: fails a kill rule. By the rules set in advance, the edge isn't there for this league "
            "and period.")


def add_common(p) -> None:
    p.add_argument("--sport", default="basketball_nba", help="Odds API sport key (default basketball_nba)")
    p.add_argument("--series", help="Kalshi series ticker (default: KXNBAGAME for the NBA, KXNFLGAME for the NFL)")
    p.add_argument("--from", dest="start", required=True, help="first game date, YYYY-MM-DD (US Eastern)")
    p.add_argument("--to", dest="end", required=True, help="last game date, YYYY-MM-DD")
    p.add_argument("--hours", type=float, default=6, help="look at trades from this many hours before the start (default 6)")
    p.add_argument("--step", type=int, default=30, help="minutes between Pinnacle snapshots (default 30)")
    p.add_argument("--db", default=DEFAULT_DB, help=f"cache file (default {DEFAULT_DB})")


def main(argv=None) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # httpx logs every request URL at INFO, and Odds API URLs contain the API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m tradeorama.replay",
                                description="Replay Kalshi sports trades against Pinnacle's no-vig line")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download and cache the data (asks before spending Odds API credits)")
    add_common(f)
    f.add_argument("--max-credits", type=int, default=6000, help="most Odds API credits this run may use (default 6000)")
    f.add_argument("--keep-credits", type=int, default=2500,
                   help="stop if fewer than this many credits would be left this month (default 2500)")
    f.add_argument("--yes", action="store_true", help="don't ask before spending credits")
    f.set_defaults(fn=cmd_fetch)
    r = sub.add_parser("report", help="replay the cached data and apply the kill rules")
    add_common(r)
    r.add_argument("--min-edge", type=float, default=0.03, help="minimum edge after fees, in dollars (default 0.03)")
    r.add_argument("--stake", type=float, default=100, help="paper stake per window in dollars (default 100)")
    r.add_argument("--il-tax", type=float, default=0.0, help="Illinois tax rate on the price, e.g. 0.0175 (default 0)")
    r.add_argument("--fee-coef", type=float, help="Kalshi taker fee coefficient (default 0.07 × the series' multiplier)")
    r.add_argument("--tolerance", type=float, default=0.005,
                   help="how far Pinnacle may move between the snapshots around a trade (default 0.005)")
    r.add_argument("--max-divergence", type=float, default=0.15, help="ignore trades this far from Pinnacle (default 0.15)")
    r.add_argument("--top", type=int, default=10, help="how many of the largest gaps to list (default 10)")
    r.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
