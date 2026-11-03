"""Command line: python -m wxbrackets {forecast,observe,backfill-outcomes,calibration,check-models}"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .brackets import make_brackets, parse_edges, round_half_up
from .calibration import calibrate
from .cli_report import best_report
from .climate_day import climate_date, climate_day_window
from .config import Config, ConfigError, Station, load_config
from .db import DB
from .forecast import build_forecast, run_record
from .http import Http
from .iem import IEM
from .nws import NWS
from .openmeteo import OpenMeteo, parse_members

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
    )


def today_for(station: Station) -> date:
    return climate_date(datetime.now(timezone.utc), station.tz)


def fmt_f(x: float | None, digits: int = 1) -> str:
    return "–" if x is None else f"{x:.{digits}f}°F"


# --- forecast ---------------------------------------------------------------------------


def cmd_forecast(cfg: Config, args) -> None:
    station = cfg.station(args.station)
    day = date.fromisoformat(args.date) if args.date else today_for(station)
    edges = parse_edges(args.brackets) if args.brackets else parse_edges(cfg.default_brackets)
    brackets = make_brackets(edges)
    http = make_http(cfg)
    nws = NWS(http)
    today = today_for(station)
    if day < today:
        print(f"Note: {day} is already over at {station.name}. Use backfill-outcomes for the official high.")

    maxes, _ = OpenMeteo(cfg, http).member_maxes(station, day, [m for m in cfg.models if m.weight > 0])

    observed = None
    if day == today:
        try:
            observed = nws.running_max(station, day).max_f
        except httpx.HTTPError as exc:
            log.warning("NWS observations unavailable, so not conditioning on today's max: %s", exc)
    try:
        nws_high = nws.forecast_high(station, day)
    except httpx.HTTPError as exc:
        log.warning("NWS point forecast unavailable: %s", exc)
        nws_high = None

    now = datetime.now(timezone.utc)
    f = build_forecast(cfg, station, day, maxes, brackets, now, observed, nws_high)
    start, end = climate_day_window(day, station.tz)

    print(f"{station.name} ({station.icao})  high for {day}")
    print(f"Climate day (local standard time): {start.astimezone(station.tz):%m-%d %H:%M %Z} to "
          f"{end.astimezone(station.tz):%m-%d %H:%M %Z}  |  lead {f.lead_hours:+.1f} h")
    print("\nModels:")
    for m in cfg.models:
        n, avg = f.per_model.get(m.id, (0, None))
        exp = f" of {m.expected_members}" if m.expected_members else ""
        flag = "" if n else "  (no members cover this day)"
        print(f"  {m.name:32} {n:3d}{exp:>6} members  mean max {fmt_f(avg)}  weight {m.weight:g}{flag}")
    print(f"\nPooled ensemble mean:   {fmt_f(f.pooled_mean_f)}")
    print(f"Distribution mean:      {fmt_f(f.dist.mean())}  (bias {cfg.distribution.bias_f:+.1f}, "
          f"spread x{cfg.distribution.spread_inflation:g}, kernel {f.dist.bandwidth:.2f}°F)")
    print(f"NWS point forecast:     {fmt_f(nws_high, 0)}")
    if observed is not None:
        print(f"Observed max so far:    {fmt_f(observed)}  (high can't finish below "
              f"{observed - cfg.distribution.obs_tolerance_f:.1f}°F)")
    print(f"\n{'bracket':>9}  {'prob':>6}  {'fair ¢':>6}")
    for b, p in zip(brackets, f.probs):
        print(f"{b.label:>9}  {100 * p:5.1f}%  {100 * p:6.1f}")
    print(f"{'total':>9}  {100 * sum(f.probs):5.1f}%")

    if not args.no_save:
        db = DB(cfg.database)
        run_id = db.save_run(run_record(f, cfg), f.members,
                             [(b.label, b.low, b.high, p) for b, p in zip(brackets, f.probs)])
        print(f"\nSaved as run {run_id} in {cfg.database}")


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
    print(f"{rm.observations} observations, latest {rm.latest.astimezone(station.tz):%H:%M %Z}")
    print("Hourly/5-minute observations can miss the true peak; the official CLI high may be a degree higher.")


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
        db.save_outcome(station.code, d, r.max_f, r.final, args.source)
        found += 1
        tag = "" if r.final else "  (same-day report only, not final)"
        print(f"{d}  high {r.max_f}°F" + (f" at {r.max_time} LST" if r.max_time else "") + tag)
    print(f"\nStored {found} of {len(days)} days in {cfg.database}")


# --- calibration ------------------------------------------------------------------------


def cmd_calibration(cfg: Config, args) -> None:
    station = cfg.station(args.station)
    res = calibrate(DB(cfg.database), station.code, args.min_lead, args.max_lead)
    if not res.runs:
        print("No forecasts with a final official high yet. Run forecasts, then backfill-outcomes.")
        return
    print(f"{station.name}: {res.runs} forecast runs over {res.days} days")
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
    for m in cfg.models:
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
    # Also check the combined requests `forecast` sends: one per API.
    for source in ("ensemble", "forecast"):
        ids = [m.id for m in cfg.models if m.source == source]
        if not ids:
            continue
        combined = om.fetch(station, day, ids, source)
        try:
            total = len(parse_members(combined, ids))
            print(f"Combined {source} request: {total} members parsed from {len(combined.get('hourly', {})) - 1} keys")
        except ValueError as exc:
            ok = False
            print(f"Combined {source} request: keys couldn't be assigned to models ({exc}). Sample keys: "
                  f"{list(combined.get('hourly', {}))[:6]}")
        if args.dump:
            (Path(args.dump) / f"combined_{source}.json").write_text(json.dumps(combined))
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
    f.add_argument("--brackets", help="upper edges, e.g. 45,47,49,51,53,55 (default from config)")
    f.add_argument("--no-save", action="store_true", help="don't log this run to the database")
    f.set_defaults(fn=cmd_forecast)

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
