"""Command line: python -m wxbrackets {forecast,observe,backfill-outcomes,calibration,check-models}"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import paper
from .brackets import make_brackets, parse_edges, round_half_up
from .calibration import LEADS, calibrate
from .cli_report import best_report
from .climate_day import climate_date, climate_day_window
from .config import Config, ConfigError, Station, load_config
from .db import DB
from .forecast import Forecast, build_forecast, run_record
from .http import Http
from .iem import IEM
from .nws import NWS
from .openmeteo import OpenMeteo, parse_members
from .paper import opportunities
from .polymarket_us import PolymarketUS
from .rrfs import DETERMINISTIC, ENSEMBLE, RRFS

log = logging.getLogger("wxbrackets")


def make_http(cfg: Config, needs_nws: bool = True) -> Http:
    if needs_nws and (not cfg.nws_user_agent or "example.com" in cfg.nws_user_agent):
        raise ConfigError("set nws.user_agent in config.yaml to something that identifies you, with contact info")
    return Http(
        cfg.nws_user_agent or "wxbrackets",
        min_interval={
            "api.weather.gov": cfg.nws_min_interval,
            "mesonet.agron.iastate.edu": 1.0,
            httpx.URL(cfg.open_meteo_url).host: cfg.open_meteo_min_interval,
            httpx.URL(cfg.open_meteo_forecast_url).host: cfg.open_meteo_min_interval,
        },
        # GRIB messages are a couple of MB each: allow time for them.
        timeout=60.0,
    )


def today_for(station: Station) -> date:
    return climate_date(datetime.now(timezone.utc), station.tz)


def fmt_f(x: float | None, digits: int = 1) -> str:
    return "–" if x is None else f"{x:.{digits}f}°F"


# --- forecast ---------------------------------------------------------------------------


@dataclass
class ForecastRun:
    forecast: Forecast
    markets: list | None  # Polymarket US bracket markets, when the station has an event that day
    run_id: int | None
    window: tuple[datetime, datetime]


def run_forecast(cfg: Config, station: Station, day: date, edges: list[int] | None, save: bool, http: Http,
                 db: DB, quiet: bool = False, rrfs: RRFS | None = None, kind: str = "high") -> ForecastRun:
    """Forecast one station-day's high or low. Brackets: `edges` if given, else that day's Polymarket
    US event, else the config."""
    nws = NWS(http)
    today = today_for(station)
    markets = None
    prefix = station.prefix(kind)
    if edges is None and prefix:
        try:
            markets = PolymarketUS(http, cfg.pmus_gateway).markets(prefix, day)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Polymarket US market unavailable: %s", exc)
    if markets:
        brackets = [m.bracket for m in markets]
    else:
        brackets = make_brackets(edges or parse_edges(cfg.default_brackets))

    # Today: observations first. With them, models only need to cover the rest of the day.
    observed = None
    if day == today:
        try:
            observed = nws.running_max(station, day).value(kind)
        except httpx.HTTPError as exc:
            log.warning("NWS observations unavailable, so not conditioning on today's %s: %s", kind, exc)
    now = datetime.now(timezone.utc)
    since = now if observed is not None else None

    active = [m for m in cfg.models if m.weight > 0]
    maxes, _ = OpenMeteo(cfg, http).member_maxes(station, day, active, since, kind)
    noaa = [m for m in active if m.source in ("rrfs", "refs")]
    if noaa:
        if not quiet:
            print("Fetching RRFS/REFS from NOAA's AWS bucket (cached after the first run)...", file=sys.stderr)
        rrfs = rrfs or RRFS(http, cache=db, base_url=cfg.rrfs_base_url)
        maxes.update(rrfs.member_maxes(station, day, noaa, now, since, kind))

    try:
        nws_high = nws.forecast_high(station, day, kind)
    except httpx.HTTPError as exc:
        log.warning("NWS point forecast unavailable: %s", exc)
        nws_high = None

    f = build_forecast(cfg, station, day, maxes, brackets, now, observed, nws_high, remaining=since is not None, kind=kind)
    run_id = None
    if save:
        run_id = db.save_run(run_record(f, cfg), f.members, [(b.label, b.low, b.high, p) for b, p in zip(brackets, f.probs)])
        if markets:
            db.save_market_prices(run_id, [(m.slug, m.bracket.label, m.bracket.low, m.bracket.high, m.yes_bid,
                                            m.yes_ask, m.fee_rate) for m in markets])
    return ForecastRun(f, markets, run_id, climate_day_window(day, station.tz))


def cents(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.0f}¢"


def print_bracket_table(fr: ForecastRun) -> None:
    f = fr.forecast
    if not fr.markets:
        print(f"\n{'bracket':>9}  {'prob':>6}  {'fair ¢':>6}")
        for b, p in zip(f.brackets, f.probs):
            print(f"{b.label:>9}  {100 * p:5.1f}%  {100 * p:6.1f}")
        print(f"{'total':>9}  {100 * sum(f.probs):5.1f}%")
        return
    print("\nPolymarket US (edges are per contract, after the taker fee):")
    print(f"{'bracket':>9}  {'model':>6}  {'yes bid':>7} {'yes ask':>7}  {'buy yes':>8}  {'buy no':>8}")
    for o_yes, o_no in zip(*[iter(opportunities(fr.markets, f.probs))] * 2):
        m = o_yes.market
        fmt = lambda e: "–" if e is None else f"{100 * e:+.1f}¢"  # noqa: E731
        print(f"{m.bracket.label:>9}  {100 * o_yes.prob:5.1f}%  {cents(m.yes_bid):>7} {cents(m.yes_ask):>7}  "
              f"{fmt(o_yes.edge):>8}  {fmt(o_no.edge):>8}")
    print(f"{'total':>9}  {100 * sum(f.probs):5.1f}%")


def cmd_forecast(cfg: Config, args) -> None:
    station = cfg.station(args.station)
    kind = args.kind
    day = date.fromisoformat(args.date) if args.date else today_for(station)
    if day < today_for(station):
        print(f"Note: {day} is already over at {station.name}. Use backfill-outcomes for the official {kind}.")
    fr = run_forecast(cfg, station, day, parse_edges(args.brackets) if args.brackets else None,
                      not args.no_save, make_http(cfg), DB(cfg.database), kind=kind)
    f = fr.forecast
    start, end = fr.window
    d = cfg.distribution_for(kind)
    stat = "max" if kind == "high" else "min"

    print(f"{station.name} ({station.icao})  {kind} for {day}")
    print(f"Climate day (local standard time): {start.astimezone(station.tz):%m-%d %H:%M %Z} to "
          f"{end.astimezone(station.tz):%m-%d %H:%M %Z}  |  lead {f.lead_hours:+.1f} h")
    print("\nModels:")
    for m in cfg.models:
        n, avg = f.per_model.get(m.id, (0, None))
        exp = f" of {m.expected_members}" if m.expected_members else ""
        flag = "" if n else "  (no members cover this day)"
        print(f"  {m.name:32} {n:3d}{exp:>6} members  mean {stat} {fmt_f(avg)}  weight {m.weight:g}{flag}")
    print(f"\nPooled ensemble mean:   {fmt_f(f.pooled_mean_f)}")
    print(f"Distribution mean:      {fmt_f(f.dist.mean())}  (bias {d.bias_f:+.1f}, "
          f"spread x{d.spread_inflation:g}, kernel {f.dist.kernel_f:.2f}°F)")
    print(f"NWS point forecast:     {fmt_f(f.nws_forecast_high_f, 0)}")
    if f.observed_max_f is not None:
        if kind == "high":
            bound = f"can't finish below {f.observed_max_f - d.obs_tolerance_f:.1f}°F"
        else:
            bound = f"can't finish above {f.observed_max_f + d.obs_tolerance_f:.1f}°F"
        print(f"Observed {stat} so far:    {fmt_f(f.observed_max_f)}  (models cover only the hours left; "
              f"the {kind} combines the two, and {bound})")
    print_bracket_table(fr)
    if fr.run_id is not None:
        print(f"\nSaved as run {fr.run_id} in {cfg.database}")


# --- paper trading ----------------------------------------------------------------------


def cmd_paper(cfg: Config, args) -> None:
    kinds = [args.kind] if args.kind else list(cfg.paper.kinds)
    linked = lambda s: any(s.prefix(k) for k in kinds)  # noqa: E731
    stations = [cfg.station(args.station)] if args.station else [s for s in cfg.stations.values() if linked(s)]
    if not stations or not all(linked(s) for s in stations):
        raise ConfigError("paper trading needs a station with pmus_event_prefix / pmus_low_prefix (a Polymarket US weather city)")
    http, db = make_http(cfg), DB(cfg.database)
    pmus = PolymarketUS(http, cfg.pmus_gateway)
    grib = None  # shared across loops, so each city's grid point is located only once
    while True:
        # One NOAA download per file serves every city: read all of their points from it.
        rrfs = RRFS(http, cache=db, base_url=cfg.rrfs_base_url, grib=grib, points=[(s.lat, s.lon) for s in stations])
        settled = paper.settle(db, pmus)
        if settled:
            print(f"Settled {settled} paper trades.")
        for station in stations:
            today = today_for(station)
            days = [date.fromisoformat(args.date)] if args.date else [today, today + timedelta(days=1)]
            for day, kind in [(d, k) for d in days for k in kinds if station.prefix(k)]:
                tag = f"{station.code} {kind:4} {day}"
                try:
                    fr = run_forecast(cfg, station, day, None, True, http, db, quiet=True, rrfs=rrfs, kind=kind)
                except (httpx.HTTPError, ValueError) as exc:
                    print(f"{tag}: skipped ({exc})")
                    continue
                if not fr.markets:
                    print(f"{tag}: no Polymarket US event")
                    continue
                blocked = paper.same_day_block(day, today, fr.window[0], datetime.now(timezone.utc),
                                               fr.forecast.observed_max_f, cfg.paper, kind)
                if blocked:
                    # Still forecast and log prices (done above): calibration's "today (shadow)" row
                    # shows whether the model beats the market there, i.e. whether to move the cutoff.
                    print(f"{tag}: no trades ({blocked}); forecast and prices logged for evaluation")
                    continue
                hours_left = (fr.window[1] - datetime.now(timezone.utc)).total_seconds() / 3600
                opps = paper.trade(db, pmus, cfg.paper, station.code, day.isoformat(), fr.run_id, fr.markets,
                                   fr.forecast.probs, hours_left, execute=not args.dry, kind=kind)
                buys = [o for o in opps if o.action == "buy"]
                best = max((o for o in opps if o.edge is not None), key=lambda o: o.edge, default=None)
                note = f"best edge {100 * best.edge:+.1f}¢ ({best.side} {best.market.bracket.label})" if best else "no quotes"
                print(f"{tag}: mean {fr.forecast.dist.mean():.1f}°F, {note}, {len(buys)} buys")
                for o in buys:
                    verb = "would buy" if args.dry else "paper buy"
                    print(f"    {verb} {o.side.upper()} {o.market.bracket.label}: {o.fill.contracts:.2f} contracts "
                          f"for ${o.fill.cost:.2f} (model {100 * o.prob:.0f}%, ask {cents(o.ask)}, "
                          f"edge {100 * o.fill.edge_per_contract:+.1f}¢)")
        grib = rrfs._grib
        if not args.every:
            return
        print(f"Next check in {args.every:g} min (Ctrl+C to stop).")
        time.sleep(args.every * 60)


def cmd_paper_report(cfg: Config, args) -> None:
    db = DB(cfg.database)
    station = args.station.upper() if args.station else None
    fills = db.paper_fills(station, args.kind)
    if not fills:
        print("No paper trades yet. Run: python -m wxbrackets paper")
        return
    groups = sorted({(f["station"], f["kind"]) for f in fills})
    print(f"{'station':8} {'kind':5} {'trades':>6} {'staked':>9} {'open':>8} {'avg edge':>9} {'settled':>8} {'P&L':>9} {'ROI':>7}")
    for code, kind in [*groups, (None, args.kind)]:
        s = paper.summary(db, code or station, kind)
        if not s["trades"]:
            continue
        roi = "–" if s["roi_pct"] is None else f"{s['roi_pct']:+.1f}%"
        edge_c = "–" if s["avg_edge_cents"] is None else f"{s['avg_edge_cents']:+.1f}¢"
        pnl = ("-" if s["settled_pnl_usd"] < 0 else "+") + "$" + format(abs(s["settled_pnl_usd"]), ".2f")
        print(f"{code or 'ALL':8} {kind or 'both':5} {s['trades']:6d} {'$' + format(s['staked_usd'], '.2f'):>9} "
              f"{'$' + format(s['open_usd'], '.2f'):>8} {edge_c:>9} {s['settled']:8d} {pnl:>9} {roi:>7}")
    print("\nRecent:")
    for f in fills[: args.limit]:
        res = "open" if f["payout"] is None else f"{f['payout'] - f['cost']:+.2f}"
        print(f"  {f['created_utc'][:16]} {f['station']} {f['kind']:4} {f['target_date']} {f['side'].upper():3} "
              f"{f['label']:>6} {f['contracts']:7.2f} for ${f['cost']:6.2f}  model {100 * f['prob']:3.0f}%  "
              f"edge {100 * f['edge']:+.1f}¢  {res}")


# --- observe ----------------------------------------------------------------------------


def cmd_observe(cfg: Config, args) -> None:
    station = cfg.station(args.station)
    day = today_for(station)
    rm = NWS(make_http(cfg)).running_max(station, day)
    start, _ = climate_day_window(day, station.tz)
    print(f"{station.name} ({station.icao}), climate day {day} (from {start.astimezone(station.tz):%H:%M %Z})")
    if rm.max_f is None:
        print("No observations yet today.")
        return
    print(f"Running max: {rm.max_f:.1f}°F at {rm.at.astimezone(station.tz):%H:%M %Z} "
          f"(reports as {round_half_up(rm.max_f)}°F if it holds)")
    print(f"Running min: {rm.min_f:.1f}°F at {rm.min_at.astimezone(station.tz):%H:%M %Z} "
          f"(reports as {round_half_up(rm.min_f)}°F if it holds)")
    print(f"{rm.observations} observations, latest {rm.latest.astimezone(station.tz):%H:%M %Z}")
    print("Hourly/5-minute observations can miss the true extremes; the official CLI high may be a degree "
          "higher and the low a degree lower.")


# --- backfill ---------------------------------------------------------------------------


def cmd_backfill(cfg: Config, args) -> None:
    station = cfg.station(args.station)
    last = today_for(station) - timedelta(days=1)
    first = last - timedelta(days=args.days - 1)
    http = make_http(cfg)
    reports = []
    if args.source in ("auto", "iem"):
        try:
            reports += IEM(http).cli_reports(station, first, last)
        except httpx.HTTPError as exc:
            log.warning("IEM archive unavailable: %s", exc)
    days = [first + timedelta(days=i) for i in range(args.days)]

    def missing():
        return [d for d in days if not (r := best_report(reports, d)) or not r.final]

    if args.source == "nws" or (args.source == "auto" and missing()):
        try:
            reports += NWS(http).recent_cli_reports(station, since=min(missing() or days))
        except httpx.HTTPError as exc:
            log.warning("NWS CLI products unavailable: %s", exc)

    db = DB(cfg.database)
    found = 0
    for d in days:
        r = best_report([x for x in reports if x.location in (None, station.cli_location)], d)
        if r is None:
            print(f"{d}  no report found")
            continue
        db.save_outcome(station.code, d, r.max_f, r.final, args.source, r.min_f)
        found += 1
        tag = "" if r.final else "  (same-day report only, not final)"
        low = "" if r.min_f is None else f", low {r.min_f}°F" + (f" at {r.min_time} LST" if r.min_time else "")
        print(f"{d}  high {r.max_f}°F" + (f" at {r.max_time} LST" if r.max_time else "") + low + tag)
    print(f"\nStored {found} of {len(days)} days in {cfg.database}")


# --- calibration ------------------------------------------------------------------------


def cmd_calibration(cfg: Config, args) -> None:
    station = cfg.station(args.station)
    res = calibrate(DB(cfg.database), station.code, args.min_lead, args.max_lead, cfg.paper.last_hour(args.kind), args.kind)
    if not res.runs:
        print(f"No forecasts with a final official {args.kind} yet. Run forecasts, then backfill-outcomes.")
        return
    print(f"{station.name} daily {args.kind}s: {res.runs} forecast runs over {res.days} days")
    print(f"Brier score (multi-category, 0 = perfect): {res.brier:.3f}")
    print(f"Ensemble mean bias (raw mean - official): {res.mean_bias_f:+.2f}°F   MAE {res.mean_abs_err_f:.2f}°F")
    print(f"Distribution mean bias (after bias_f):    {res.dist_bias_f:+.2f}°F")
    if res.nws_bias_f is not None:
        print(f"NWS point forecast bias:                  {res.nws_bias_f:+.2f}°F")
    if not res.per_model.empty:
        print("\nBy model (each model's own mean max vs. official):")
        print(f"{'model':24} {'runs':>5} {'bias':>8} {'MAE':>7}")
        for row in res.per_model.itertuples():
            print(f"{row.model:24} {row.runs:5d} {row.bias_f:+7.2f}° {row.mae_f:6.2f}°")
        print("\nBy model and lead time (bias / MAE, runs): which models to trust at which distance from the day")
        print(f"{'model':24} " + " ".join(f"{lead:>20}" for lead in LEADS))
        cells = {(r.model, r.lead): f"{r.bias_f:+.2f} / {r.mae_f:.2f} ({r.runs})" for r in res.per_model_lead.itertuples()}
        for model in res.per_model.model:
            print(f"{model:24} " + " ".join(f"{cells.get((model, lead), '–'):>20}" for lead in LEADS))
        print("Same-day errors are of each model's final high (observed so far combined with its forecast for the"
              " hours left).")
    if res.market_runs:
        better = "better" if res.model_brier_vs_market < res.market_brier else "worse"
        print(f"\nModel vs. Polymarket US on the {res.market_runs} runs with market prices (lower is better):")
        print(f"  model Brier {res.model_brier_vs_market:.3f}   market Brier {res.market_brier:.3f}   -> model is {better}")
        print("  Market probabilities are each bracket's mid price, scaled so the brackets sum to 100%.")
        print(f"\n  {'lead':12} {'runs':>5} {'model':>7} {'market':>7}")
        for lead in LEADS:
            if lead in res.market_by_lead:
                n, mb, kb = res.market_by_lead[lead]
                note = "  (not traded: tells whether to move the same-day cutoff)" if lead == "today (shadow)" else ""
                print(f"  {lead:12} {n:5d} {mb:7.3f} {kb:7.3f}  {'model better' if mb < kb else 'market better'}{note}")
    print(f"\nTo remove the raw bias, set distribution.bias_f to about {-res.mean_bias_f:+.1f}"
          " (needs many days to be meaningful).")
    print("\nReliability (bracket probabilities vs. how often they hit):")
    print(f"{'forecast bin':>14} {'count':>6} {'avg forecast':>13} {'observed':>9}")
    for row in res.reliability.itertuples():
        print(f"{str(row.bin):>14} {row.count:6d} {row.mean_forecast:13.2f} {row.observed_freq:9.2f}")


# --- check-models -----------------------------------------------------------------------


def cmd_check_models(cfg: Config, args) -> None:
    station = cfg.station(args.station)
    day = today_for(station) + timedelta(days=1)
    om = OpenMeteo(cfg, make_http(cfg, needs_nws=False))
    ok = True
    noaa = RRFS(make_http(cfg, needs_nws=False), base_url=cfg.rrfs_base_url)
    for m in cfg.models:
        if m.source == "rrfs":
            print(f"{m.id:24} {noaa.check(DETERMINISTIC, station, day, 0)}")
            continue
        if m.source == "refs":
            for member in m.members or range(1, 6):
                print(f"{m.id + f' m{member:03d}':24} {noaa.check(ENSEMBLE, station, day, member)}")
            continue
        try:
            payload = om.fetch(station, day, [m.id], m.source)
        except httpx.HTTPStatusError as exc:
            print(f"{m.id:24} ERROR {exc.response.status_code}: {exc.response.text[:200]}")
            ok = False
            continue
        keys = [k for k in (payload.get("hourly") or {}) if k != "time"]
        n = len(parse_members(payload, [m.id]))
        expected = m.expected_members
        status = "ok" if expected in (None, n) else f"MISMATCH (expected {expected})"
        ok &= status == "ok"
        print(f"{m.id:24} {n:3d} members  {status}   keys e.g. {keys[:2]}")
        if args.dump:
            Path(args.dump).mkdir(parents=True, exist_ok=True)
            (Path(args.dump) / f"{m.id}.json").write_text(json.dumps(payload))
    if not ok:
        sys.exit("Fix the model ids or expected_members in config.yaml (see https://open-meteo.com/en/docs/ensemble-api).")


def main(argv=None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m wxbrackets", description="Daily high-temperature bracket probabilities")
    p.add_argument("--config", default="config.yaml", help="YAML config (default config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("forecast", help="probabilities for each bracket of a day's high")
    f.add_argument("--station", required=True)
    f.add_argument("--date", help="YYYY-MM-DD (default: today's climate day)")
    f.add_argument("--brackets", help="upper edges, e.g. 45,47,49,51,53,55 (default: the day's Polymarket US "
                   "brackets if the station has them, else the config)")
    f.add_argument("--kind", choices=["high", "low"], default="high", help="the day's high (default) or low")
    f.add_argument("--no-save", action="store_true", help="don't log this run to the database")
    f.set_defaults(fn=cmd_forecast)

    pp = sub.add_parser("paper", help="paper trade Polymarket US weather brackets (no real orders)")
    pp.add_argument("--station", help="one station (default: every station with a Polymarket US event)")
    pp.add_argument("--date", help="YYYY-MM-DD (default: today and tomorrow)")
    pp.add_argument("--every", type=float, help="repeat every N minutes")
    pp.add_argument("--dry", action="store_true", help="show what it would buy without recording anything")
    pp.add_argument("--kind", choices=["high", "low"], help="only highs or only lows (default: paper.kinds)")
    pp.set_defaults(fn=cmd_paper)

    pr = sub.add_parser("paper-report", help="paper trading results")
    pr.add_argument("--station")
    pr.add_argument("--limit", type=int, default=20)
    pr.add_argument("--kind", choices=["high", "low"])
    pr.set_defaults(fn=cmd_paper_report)

    o = sub.add_parser("observe", help="today's running max from NWS observations")
    o.add_argument("--station", required=True)
    o.set_defaults(fn=cmd_observe)

    b = sub.add_parser("backfill-outcomes", help="store official CLI highs for past days")
    b.add_argument("--station", required=True)
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--source", choices=["auto", "nws", "iem"], default="auto",
                   help="auto: IEM archive, then api.weather.gov for anything missing")
    b.set_defaults(fn=cmd_backfill)

    c = sub.add_parser("calibration", help="score logged forecasts against official highs")
    c.add_argument("--station", required=True)
    c.add_argument("--min-lead", type=float, help="only runs at least this many hours before the day starts")
    c.add_argument("--max-lead", type=float, help="only runs at most this many hours before the day starts")
    c.add_argument("--kind", choices=["high", "low"], default="high", help="score high (default) or low forecasts")
    c.set_defaults(fn=cmd_calibration)

    m = sub.add_parser("check-models", help="confirm each configured Open-Meteo model returns its members")
    m.add_argument("--station", required=True)
    m.add_argument("--dump", metavar="DIR", help="save the raw JSON responses here")
    m.set_defaults(fn=cmd_check_models)

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
        args.fn(cfg, args)
    except (ConfigError, ValueError) as exc:
        sys.exit(f"error: {exc}")
    except httpx.HTTPError as exc:
        sys.exit(f"network error: {exc}")
