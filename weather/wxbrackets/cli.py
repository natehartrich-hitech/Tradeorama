"""Command line: python -m wxbrackets {forecast,observe,backfill-outcomes,calibration,check-models}"""

import argparse
import json
import logging
import sys
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
from .heartbeat import Heartbeat
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

    weights = cfg.weights_for(kind, station)
    active = [m for m in cfg.models if weights[m.id] > 0]
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


def fmt_usd(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):.2f}"


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
    d = cfg.distribution_for(kind, station)
    weights = cfg.weights_for(kind, station)
    stat = "max" if kind == "high" else "min"

    print(f"{station.name} ({station.icao})  {kind} for {day}")
    print(f"Climate day (local standard time): {start.astimezone(station.tz):%m-%d %H:%M %Z} to "
          f"{end.astimezone(station.tz):%m-%d %H:%M %Z}  |  lead {f.lead_hours:+.1f} h")
    print("\nModels:")
    for m in cfg.models:
        n, avg = f.per_model.get(m.id, (0, None))
        exp = f" of {m.expected_members}" if m.expected_members else ""
        flag = "" if n else "  (no members cover this day)"
        print(f"  {m.name:32} {n:3d}{exp:>6} members  mean {stat} {fmt_f(avg)}  weight {weights[m.id]:g}{flag}")
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
    # Pings a dead man's switch (config `heartbeat_url`) so a bot that stops, crashes or hangs
    # raises an alert. Dry runs don't ping: they'd hide a stopped bot.
    hb = Heartbeat(None if args.dry else cfg.heartbeat_url)
    try:
        paper_loop(cfg, args, hb)
    except Exception as exc:
        hb.fail(f"wxbrackets paper stopped: {exc!r}")
        raise


def paper_loop(cfg: Config, args, hb: Heartbeat) -> None:
    kinds = [args.kind] if args.kind else list(cfg.paper.kinds)
    linked = lambda s: any(s.prefix(k) for k in kinds)  # noqa: E731
    stations = [cfg.station(args.station)] if args.station else [s for s in cfg.stations.values() if linked(s)]
    if not stations or not all(linked(s) for s in stations):
        raise ConfigError("paper trading needs a station with pmus_event_prefix / pmus_low_prefix (a Polymarket US weather city)")
    http, db = make_http(cfg), DB(cfg.database)
    pmus = PolymarketUS(http, cfg.pmus_gateway)
    grib = None  # shared across loops, so each city's grid point is located only once
    while True:
        if args.every:  # timestamps make a long-running log readable
            print(f"--- check at {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} ---")
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
        hb.beat(force=True)  # a full pass finished
        if not args.every:
            return
        print(f"Next check in {args.every:g} min (Ctrl+C to stop).")
        hb.sleep(args.every * 60)


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
    luck = paper.luck_check(db, station, args.kind)
    if luck:
        print(f"\nIs it luck? {luck.positions} settled positions, P&L {fmt_usd(luck.pnl)}:")
        print(f"  If the model's odds were right: expected {fmt_usd(luck.model_expected)}, "
              f"typically within ±${2 * luck.model_sd:.2f}")
        print(f"  If the market's odds were right (no skill): expected about $0.00, typically within ±${2 * luck.null_sd:.2f}")
        print(f"  With no skill, a result at least this good happens about {luck.chance_pct:.0f}% of the time"
              + (" -> unlikely to be luck" if luck.chance_pct < 5 else " -> can't rule out luck yet"))
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
        print_brier_margin(res)
        print(f"\n  {'lead':12} {'runs':>5} {'model':>7} {'market':>7}")
        for lead in LEADS:
            if lead in res.market_by_lead:
                n, mb, kb = res.market_by_lead[lead]
                note = "  (not traded: tells whether to move the same-day cutoff)" if lead == "today (shadow)" else ""
                print(f"  {lead:12} {n:5d} {mb:7.3f} {kb:7.3f}  {'model better' if mb < kb else 'market better'}{note}")
    print_bias_suggestion(cfg, station, args.kind, res)
    print("\nReliability (bracket probabilities vs. how often they hit):")
    print(f"{'forecast bin':>14} {'count':>6} {'avg forecast':>13} {'observed':>9}")
    for row in res.reliability.itertuples():
        print(f"{str(row.bin):>14} {row.count:6d} {row.mean_forecast:13.2f} {row.observed_freq:9.2f}")


def print_brier_margin(res) -> None:
    """Model minus market Brier, with a ~95% range: is "model better" clear, or still a coin flip?"""
    if res.brier_diff is None:
        return
    if res.brier_diff_se is None:
        print(f"  Difference (model - market): {res.brier_diff:+.3f} from 1 day; a margin of error needs at least 2.")
        return
    lo, hi = res.brier_diff - 2 * res.brier_diff_se, res.brier_diff + 2 * res.brier_diff_se
    if hi < 0:
        verdict = "model clearly better"
    elif lo > 0:
        verdict = "market clearly better"
    else:
        verdict = "not clear yet: the range includes zero"
    print(f"  Difference (model - market): {res.brier_diff:+.3f}, likely between {lo:+.3f} and {hi:+.3f} "
          f"over {res.brier_days} days -> {verdict}")


MIN_DAYS_FOR_CITY_SETTINGS = 30


def print_bias_suggestion(cfg: Config, station: Station, kind: str, res) -> None:
    """A ready-to-paste per-city correction, with how much data it rests on."""
    current = cfg.distribution_for(kind, station)
    section = "distribution" if kind == "high" else "distribution_low"
    own = station.overrides.get(kind)
    source = f"this city's own {section}" if own else "the shared settings"
    print(f"\nCorrection in use for {station.code} {kind}s: bias_f {current.bias_f:+.1f} (from {source})")
    suggested = round(-res.mean_bias_f, 1)  # the raw ensemble error, measured before any correction
    print(f"Suggested for {station.code} {kind}s: bias_f {suggested:+.1f}, from {res.days} settled days. In config.yaml:")
    print(f"  stations:\n    {station.code}:\n      {section}:\n        bias_f: {suggested}")
    if res.days < MIN_DAYS_FOR_CITY_SETTINGS:
        print(f"Only {res.days} days so far: with day-to-day errors of 2-3°F, wait for about "
              f"{MIN_DAYS_FOR_CITY_SETTINGS} before trusting a city's own correction (until then it mostly chases noise).")


# --- backtest -----------------------------------------------------------------------------


def cmd_backtest(cfg: Config, args) -> None:
    from . import backtest as bt
    from .archive import MODELS

    kinds = args.kinds.split(",")
    model_ids = args.models.split(",")
    bad = [m for m in model_ids if m not in MODELS]
    if bad:
        raise ConfigError(f"unknown backtest model(s) {', '.join(bad)}; available: {', '.join(MODELS)}")
    codes = args.stations.split(",") if args.stations else [s.code for s in cfg.stations.values() if s.pmus_event_prefix]
    stations = [cfg.station(c) for c in codes]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=2)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    db_path = Path(args.db) if args.db else cfg.database.parent / "backtest.db"
    db = DB(db_path)

    print(f"Backtest: {len(stations)} cities ({', '.join(codes)}), {', '.join(kinds)}, {start} to {end} ({len(days)} days)")
    print(f"Decision: {args.hours_before:g} h before 00:00 UTC the evening before each day "
          "(about 6 PM Chicago, 7 PM New York, 4 PM Los Angeles), using only runs published by then.")
    late = [f"{MODELS[m].name} from {MODELS[m].first_day}" for m in model_ids if MODELS[m].first_day > start]
    if late:
        print("Archive starts later for: " + "; ".join(late) + " (earlier days run without them).")
    cases = bt.plan(stations, kinds, days, model_ids, args.hours_before)
    est = bt.estimate(cases, db, stations)
    total_gb = sum(gb for _, gb in est.values())
    print("\nStill to download (cached messages are skipped, so an interrupted run resumes):")
    for mid in model_ids:
        n, gb = est.get(mid, (0, 0.0))
        print(f"  {MODELS[mid].name:24} {n:8d} messages  ~{gb:6.1f} GB")
    print(f"  {'total':24} {sum(n for n, _ in est.values()):8d} messages  ~{total_gb:6.1f} GB")
    if args.estimate:
        return

    client = httpx.Client(timeout=60, headers={"User-Agent": cfg.nws_user_agent or "wxbrackets"},
                          limits=httpx.Limits(max_connections=args.workers * 2))
    if est:
        print("\nDownloading model data...")
        bt.download(cases, db, stations, client, args.workers)
    print("\nFetching Polymarket US markets and decision-time prices...")
    bt.load_markets(cases, db, client, cfg.pmus_gateway)

    print("Fetching official highs/lows (NWS climate reports, IEM archive)...")
    http = make_http(cfg)
    for st in stations:
        try:
            reports = [r for r in IEM(http).cli_reports(st, start, end) if r.location in (None, st.cli_location)]
        except httpx.HTTPError as exc:
            print(f"  {st.code}: archive unavailable ({exc}); fitting corrections to the winning brackets instead")
            continue
        for c in cases:
            if c.station.code == st.code and (r := best_report(reports, c.day, c.kind)) and r.final:
                c.official = r.value(c.kind)

    rows = bt.run(cfg, cases, db, model_ids, args.csv)
    print(f"\nEach model's bias (forecast minus actual; + = too warm), refitted every day from the previous "
          f"{bt.BIAS_DAYS} days, as average and range; how far it still missed after correction; and the spread "
          "widening that scored best on the earlier half:")
    for r in rows:
        print(f"  {r['station']:5} {r['kind']:4} spread "
              + ("–" if r["spread"] is None else f"x{r['spread']:g}")
              + (" (from winning brackets: official archive unavailable)" if r["truth"] == "bracket" else ""))
        for m, (avg, lo, hi) in sorted(r["model_bias"].items()):
            miss = r["model_miss"].get(m)
            print(f"      {m:5} bias {avg:+5.1f}° ({lo:+.1f}..{hi:+.1f})   still misses by "
                  + ("–" if miss is None else f"{miss:.1f}°") + " on average")
    print(f"\nResults on each city's LATER half of corrected days (the first ~{bt.MIN_BIAS_DAYS + bt.TRUTH_LAG_DAYS} days "
          f"of each only build up history), {', '.join(MODELS[m].id for m in model_ids)}:")
    print(f"{'city':5} {'kind':4} {'days':>5} {'raw bias':>9}  {'Brier model':>11} {'market':>7}  "
          f"{'difference (95% range)':34} {'trades':>6} {'P&L':>9} {'luck':>6}")
    all_positions = []
    for r in rows:
        bias = "–" if r["raw_bias"] is None else f"{r['raw_bias']:+.2f}°"
        if r["brier_diff"] is None:
            diff = "no market prices"
        elif r.get("brier_diff_se") is None:
            diff = f"{r['brier_diff']:+.3f} (1 day)"
        else:
            lo, hi = r["brier_diff"] - 2 * r["brier_diff_se"], r["brier_diff"] + 2 * r["brier_diff_se"]
            verdict = "model better" if hi < 0 else "market better" if lo > 0 else "not clear"
            diff = f"{r['brier_diff']:+.3f} ({lo:+.3f}..{hi:+.3f}) {verdict}"
        mb = "–" if r["model_brier"] is None else f"{r['model_brier']:.3f}"
        kb = "–" if r["market_brier"] is None else f"{r['market_brier']:.3f}"
        luck = "–" if r["luck"] is None else f"{r['luck'].chance_pct:.0f}%"
        print(f"{r['station']:5} {r['kind']:4} {r['test_days']:5d} {bias:>9}  {mb:>11} {kb:>7}  {diff:34} "
              f"{r['trades']:6d} {fmt_usd(r['pnl']):>9} {luck:>6}")
        all_positions += [{"contracts": t["contracts"], "cost": t["cost"], "payout": t["payout"], "prob": t["prob"]}
                          for s in r["scored"] for t in s.trades]
    if all_positions:
        overall = paper.luck_from_positions(all_positions)
        print(f"\nAll cities: {overall.positions} simulated trades, P&L {fmt_usd(overall.pnl)} "
              f"(model expected {fmt_usd(overall.model_expected)}); with no skill, at least this good "
              f"{overall.chance_pct:.0f}% of the time.")
    w_all = rows[0]["blend_w"] if rows else None
    print("\nDoes the model add anything the market doesn't already know? Blend = market and model "
          "probabilities combined; the model's weight was fitted on the EARLIER half "
          "(0 = ignore the model, 1 = trust only the model).")
    if w_all is None:
        print("  No days with full market prices to fit on.")
    else:
        print(f"  Model weight fitted on all cities together: {w_all:.2f}"
              + ("  -> the model added nothing; the blend is just the market." if w_all == 0 else ""))
        print(f"{'city':5} {'kind':4} {'own weight':>10}  {'Brier blend':>11} {'market':>7}  "
              f"{'difference (95% range)':34} {'trades':>6} {'P&L':>9} {'luck':>6}")
        blend_positions = []
        for r in rows:
            own = "–" if r["blend_own_w"] is None else f"{r['blend_own_w']:.2f}"
            if r["blend_diff"] is None:
                diff = "no market prices"
            elif r.get("blend_diff_se") is None:
                diff = f"{r['blend_diff']:+.3f} (1 day)"
            elif r["blend_diff"] == 0 and r["blend_diff_se"] == 0:
                diff = "+0.000 (same as market)"
            else:
                lo, hi = r["blend_diff"] - 2 * r["blend_diff_se"], r["blend_diff"] + 2 * r["blend_diff_se"]
                verdict = "blend better" if hi < 0 else "market better" if lo > 0 else "not clear"
                diff = f"{r['blend_diff']:+.3f} ({lo:+.3f}..{hi:+.3f}) {verdict}"
            bb = "–" if r["blend_brier"] is None else f"{r['blend_brier']:.3f}"
            kb = "–" if r["blend_market_brier"] is None else f"{r['blend_market_brier']:.3f}"
            luck = "–" if r["blend_luck"] is None else f"{r['blend_luck'].chance_pct:.0f}%"
            print(f"{r['station']:5} {r['kind']:4} {own:>10}  {bb:>11} {kb:>7}  {diff:34} "
                  f"{r['blend_trades']:6d} {fmt_usd(r['blend_pnl']):>9} {luck:>6}")
            blend_positions += [{"contracts": t["contracts"], "cost": t["cost"], "payout": t["payout"], "prob": t["prob"]}
                                for s in r["scored"] for t in s.blend_trades]
        if blend_positions:
            o = paper.luck_from_positions(blend_positions)
            print(f"Trading on the blend: {o.positions} simulated trades, P&L {fmt_usd(o.pnl)} "
                  f"(blend expected {fmt_usd(o.model_expected)}); with no skill, at least this good "
                  f"{o.chance_pct:.0f}% of the time.")
        else:
            print("Trading on the blend: no trades (never enough edge after fees).")
        print("own weight = each city's best weight on its own earlier days (fewer days, so noisier); "
              "the table uses the all-city weight.")
    print("raw bias = how far the uncorrected pooled forecast ran from the actual value, over all days "
          "(+ = too warm); the daily per-model corrections above remove it.")
    print("luck = how often a no-skill trader would do at least as well with the same trades (under ~5%: unlikely luck).")
    print("Simulated fills use the best price at the decision time; the history has no order-book depth.")
    if args.csv:
        print(f"Per-day, per-bracket details: {args.csv}")


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

    bk = sub.add_parser("backtest", help="replay archived model runs against Polymarket US's past prices")
    bk.add_argument("--start", default="2026-04-23", help="first day (default: when Polymarket US weather markets began)")
    bk.add_argument("--end", help="last day (default: two days ago)")
    bk.add_argument("--stations", help="comma-separated (default: every Polymarket US weather city)")
    bk.add_argument("--kinds", default="high,low")
    bk.add_argument("--models", default="hrrr,gefs,rrfs,refs",
                    help="from hrrr, rrfs, refs, gefs, ecmwf (ecmwf is large: check --estimate first)")
    bk.add_argument("--hours-before", type=float, default=0.0, help="decision time: hours before 00:00 UTC the evening before")
    bk.add_argument("--workers", type=int, default=8, help="parallel downloads")
    bk.add_argument("--estimate", action="store_true", help="only show how much would be downloaded")
    bk.add_argument("--db", help="cache of downloaded values and prices, separate from paper trading "
                    "(default: backtest.db in the same folder as config.yaml's database)")
    bk.add_argument("--csv", default="backtest_results.csv")
    bk.set_defaults(fn=cmd_backtest)

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
