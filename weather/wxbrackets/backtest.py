"""Backtest: replay archived model runs against Polymarket US's past prices and results.

For each day, the forecast is made at a fixed decision time the evening before (00:00 UTC minus
`hours_before`: about 6 PM Chicago, 7 PM New York, 4 PM Los Angeles), from the newest run of each
model that was published by then. The market's best prices at that minute come from Polymarket
US's price history, and the result from its resolved markets (settled on the NWS climate report).

Corrections are fitted on the EARLIER half of each city/kind's days and judged on the LATER half,
so the reported results aren't flattered by fitting and scoring on the same days.
"""

import csv
import dataclasses
import json
import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
import numpy as np

from .archive import MASKS, MESSAGE_MB, MODELS, ArchiveModel, Piece, byte_range_for, index_url
from .brackets import Bracket
from .calibration import brier_difference
from .climate_day import climate_day_window
from .config import Config, Model, Station
from .db import DB
from .forecast import build_forecast
from .paper import edge as trade_edge, fee_per_contract, kelly_fraction, luck_from_positions
from .polymarket_us import BracketMarket, event_slug, parse_event
from .rrfs import GribPoint, kelvin_to_f

log = logging.getLogger(__name__)

BACKTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_events (slug TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bt_prices (
    slug TEXT NOT NULL, decision_ts INTEGER NOT NULL, yes_ask REAL, no_ask REAL,
    PRIMARY KEY (slug, decision_ts)
);
"""


@dataclass
class Case:
    station: Station
    kind: str
    day: date
    decision: datetime
    window: tuple[datetime, datetime]
    pieces: dict = field(default_factory=dict)  # (model, member) -> [Piece]
    markets: list[BracketMarket] | None = None
    prices: dict = field(default_factory=dict)  # slug -> (yes_ask, no_ask) at the decision time
    official: int | None = None


def decision_time(day: date, hours_before: float) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=hours_before)


def plan(stations: list[Station], kinds: list[str], days: list[date], model_ids: list[str],
         hours_before: float) -> list[Case]:
    """Every station/kind/day to score, with the archived messages each model member needs."""
    cases = []
    for day in days:
        decision = decision_time(day, hours_before)
        todo = [(st, k) for st in stations for k in kinds if st.prefix(k)]
        if not todo:
            continue
        windows = {st.code: climate_day_window(day, st.tz) for st, _ in todo}
        latest_end = max(e for _, e in windows.values())
        runs = {mid: MODELS[mid].run_for(decision, latest_end) for mid in model_ids}
        for st, kind in todo:
            case = Case(st, kind, day, decision, windows[st.code])
            for mid, init in runs.items():
                if init is None:
                    continue
                model = MODELS[mid]
                for member in model.members:
                    case.pieces[(mid, member)] = model.pieces(init, member, *case.window, kind)
            cases.append(case)
    return cases


def unique_pieces(cases: list[Case]) -> dict[tuple[str, str], Piece]:
    out = {}
    for c in cases:
        for pieces in c.pieces.values():
            for p in pieces:
                out.setdefault((p.url, p.key), p)
    return out


def estimate(cases: list[Case], db: DB, stations: list[Station]) -> dict[str, tuple[int, float]]:
    """Per model: (messages still to download, approximate GB)."""
    first = stations[0]
    todo = defaultdict(int)
    for (url, key), p in unique_pieces(cases).items():
        if db.get_point(url, key, first.lat, first.lon) is None:
            todo[model_of(url)] += 1
    return {m: (n, n * MESSAGE_MB[m] / 1024) for m, n in todo.items()}


def model_of(url: str) -> str:
    for mid, frag in (("hrrr", "noaa-hrrr"), ("refs", "rrfsens."), ("rrfs", "noaa-rrfs"), ("gefs", "noaa-gefs"),
                      ("ecmwf", "ecmwf-forecasts")):
        if frag in url:
            return mid
    return "?"


def download(cases: list[Case], db: DB, stations: list[Station], client: httpx.Client, workers: int = 8,
             progress=print) -> int:
    """Fetch every uncached message once, read all stations' values from it, cache them. Resumable."""
    points = [(s.lat, s.lon) for s in stations]
    by_url: dict[str, list[Piece]] = defaultdict(list)
    for (url, key), p in unique_pieces(cases).items():
        if db.get_point(url, key, *points[0]) is None:
            by_url[url].append(p)
    if not by_url:
        return 0
    kinds = {url: ("ecmwf" if model_of(url) == "ecmwf" else "wgrib2") for url in by_url}

    def fetch(url: str) -> list[tuple[Piece, bytes | None]]:
        try:
            r = client.get(index_url(url, kinds[url]))
            if r.status_code != 200:
                return [(p, None) for p in by_url[url]]
            out = []
            for p in by_url[url]:
                rng = byte_range_for(r.text, p.selector)
                if rng is None:
                    out.append((p, None))
                    continue
                g = client.get(url, headers={"Range": f"bytes={rng[0]}-{'' if rng[1] is None else rng[1]}"})
                out.append((p, g.content if g.status_code in (200, 206) else None))
            return out
        except httpx.HTTPError as exc:
            log.warning("download failed for %s: %s", url, exc)
            return [(p, None) for p in by_url[url]]

    grib = GribPoint()
    for mid in sorted({model_of(u) for u in by_url}):
        mask = fetch_message(client, *MASKS[mid]) if mid in MASKS else None
        if mask:
            grib.set_mask(mask)
        else:
            log.warning("no land-sea mask for %s: using the nearest grid point, which may be over water", mid)
    done = missing = 0
    total = sum(len(v) for v in by_url.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for results in pool.map(fetch, list(by_url)):
            for piece, data in results:
                done += 1
                if data is None:
                    missing += 1
                    continue
                try:
                    values = grib.values(data, points)
                except Exception as exc:  # a corrupt or unexpected message: skip it, keep going
                    log.warning("couldn't decode %s %s: %s", piece.url, piece.key, exc)
                    missing += 1
                    continue
                for (lat, lon), v in zip(points, values):
                    if not math.isnan(v):
                        db.put_point(piece.url, piece.key, lat, lon, kelvin_to_f(v))
            if done and (done % 500 < len(results) or done == total):
                progress(f"  {done}/{total} messages ({missing} unavailable)")
    return done - missing


def fetch_message(client: httpx.Client, url: str, selector: tuple, kind: str) -> bytes | None:
    try:
        r = client.get(index_url(url, kind))
        rng = byte_range_for(r.text, selector) if r.status_code == 200 else None
        if rng is None:
            return None
        g = client.get(url, headers={"Range": f"bytes={rng[0]}-{'' if rng[1] is None else rng[1]}"})
        return g.content if g.status_code in (200, 206) else None
    except httpx.HTTPError:
        return None


def member_values(case: Case, db: DB) -> dict[tuple[str, int], float]:
    """Each member's daily high/low from cached messages; members missing data or coverage are dropped."""
    start, end = case.window
    pick = max if case.kind == "high" else min
    out = {}
    for (mid, member), pieces in case.pieces.items():
        vals, first, last = [], None, None
        for p in pieces:
            v = db.get_point(p.url, p.key, case.station.lat, case.station.lon)
            if v is None:
                continue
            vals.append(v)
            first = p.start if first is None else min(first, p.start)
            last = p.end if last is None else max(last, p.end)
        if len(vals) >= 4 and first <= start + timedelta(hours=3) and last >= end - timedelta(hours=3):
            out[(mid, member)] = pick(vals)
    return out


# --- market side ----------------------------------------------------------------------------


def load_markets(cases: list[Case], db: DB, client: httpx.Client, gateway: str, progress=print) -> None:
    db.conn.executescript(BACKTEST_SCHEMA)
    for i, c in enumerate(cases, 1):
        slug = event_slug(c.station.prefix(c.kind), c.day)
        row = db.conn.execute("SELECT json FROM bt_events WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            r = client.get(f"{gateway}/v1/events/slug/{slug}")
            if r.status_code != 200:
                continue
            event = r.json().get("event") or {}
            if event.get("closed"):  # only cache settled events: open ones still change
                with db.conn:
                    db.conn.execute("INSERT OR REPLACE INTO bt_events VALUES (?, ?)", (slug, json.dumps(event)))
        else:
            event = json.loads(row[0])
        try:
            markets = parse_event(event)
        except ValueError:
            continue
        if any(m.yes_payout is None for m in markets):
            continue  # not settled
        c.markets = markets
        ts = int(c.decision.timestamp())
        for m in markets:
            hit = db.conn.execute("SELECT yes_ask, no_ask FROM bt_prices WHERE slug = ? AND decision_ts = ?",
                                  (m.slug, ts)).fetchone()
            if hit is None:
                r = client.get(f"{gateway}/v1/price-history", params={
                    "symbol": m.slug, "timestamp.startTimestamp": ts - 3 * 3600,
                    "timestamp.endTimestamp": ts, "fidelity": 1})
                pts = [p for p in (r.json().get("history") or []) if int(p["timestamp"]) <= ts] if r.status_code == 200 else []
                last = pts[-1] if pts else {}
                hit = (last.get("longPrice"), last.get("shortPrice"))
                with db.conn:
                    db.conn.execute("INSERT OR REPLACE INTO bt_prices VALUES (?, ?, ?, ?)", (m.slug, ts, *hit))
            c.prices[m.slug] = tuple(hit)
        if i % 100 == 0:
            progress(f"  market data: {i}/{len(cases)}")


# --- scoring --------------------------------------------------------------------------------


def backtest_config(cfg: Config, model_ids: list[str]) -> Config:
    models = [Model(MODELS[m].id, MODELS[m].name, MODELS[m].weight, len(MODELS[m].members)) for m in model_ids]
    return dataclasses.replace(cfg, models=models)


SPREADS = (1.0, 1.3, 1.6, 2.0, 2.5, 3.0)


def plain(station: Station, kind: str, bias: float, spread: float | None = None) -> Station:
    """The station without its live per-city settings, with the backtest's fitted corrections."""
    own = {"bias_f": bias} | ({"spread_inflation": spread} if spread is not None else {})
    return dataclasses.replace(station, overrides={kind: own}, weight_overrides={})


@dataclass
class Scored:
    case: Case
    probs: list[float]
    market: list[float] | None
    hit: int  # index of the bracket that won
    pooled_mean: float
    trades: list[dict] = field(default_factory=list)


def score_case(cfg: Config, case: Case, maxes: dict, bias: float, spread: float | None = None) -> Scored | None:
    brackets: list[Bracket] = [m.bracket for m in case.markets]
    hit = next((i for i, m in enumerate(case.markets) if m.yes_payout == 1.0), None)
    if hit is None or not maxes:
        return None
    f = build_forecast(cfg, plain(case.station, case.kind, bias, spread), case.day, maxes, brackets, case.decision,
                       kind=case.kind)
    mids = []
    for m in case.markets:
        yes, no = case.prices.get(m.slug, (None, None))
        quotes = [q for q in (yes, None if no is None else 1 - no) if q is not None]
        mids.append(sum(quotes) / len(quotes) if quotes else None)
    market = None
    if all(x is not None for x in mids) and sum(mids) > 0:
        market = [x / sum(mids) for x in mids]
    return Scored(case, f.probs, market, hit, f.pooled_mean_f)


def brier(probs: list[float], hit: int) -> float:
    return sum((p - (i == hit)) ** 2 for i, p in enumerate(probs))


def simulate(scored: list[Scored], paper_cfg) -> None:
    """Paper-trade the decision-time prices: same rules as `paper` (min edge, quarter Kelly,
    per-event and daily caps), filled at the quoted price. No order-book depth in the history,
    so fills assume the small sizes these caps allow were available."""
    by_day = defaultdict(list)
    for s in scored:
        by_day[s.case.day].append(s)
    for day in sorted(by_day):
        spent_day = 0.0
        candidates = []
        for s in by_day[day]:
            for m, p in zip(s.case.markets, s.probs):
                yes, no = s.case.prices.get(m.slug, (None, None))
                for side, prob, ask in (("yes", p, yes), ("no", 1 - p, no)):
                    if ask is None or not 0 < ask < 1:
                        continue
                    e = trade_edge(prob, ask, m.fee_rate)
                    if e >= paper_cfg.min_edge:
                        candidates.append((e, s, m, side, prob, ask))
        spent_event = defaultdict(float)
        for e, s, m, side, prob, ask in sorted(candidates, key=lambda x: -x[0]):
            unit = ask + fee_per_contract(ask, m.fee_rate)
            target = kelly_fraction(prob, unit) * paper_cfg.kelly_multiplier * paper_cfg.bankroll_usd
            ev_key = (s.case.station.code, s.case.kind)
            budget = min(target, paper_cfg.max_event_usd - spent_event[ev_key], paper_cfg.max_daily_usd - spent_day)
            if budget < 1:
                continue
            contracts = int(budget / unit * 100) / 100
            cost = contracts * unit
            won = m.yes_payout == 1.0 if side == "yes" else m.yes_payout == 0.0
            s.trades.append({"bracket": m.bracket.label, "side": side, "contracts": contracts, "cost": cost,
                             "prob": prob, "edge": e, "payout": contracts if won else 0.0})
            spent_event[ev_key] += cost
            spent_day += cost


def truth(case: Case) -> tuple[float | None, str]:
    """The official value if known; else the winning bracket's midpoint (within about a degree)."""
    if case.official is not None:
        return float(case.official), "official"
    won = next((m.bracket for m in case.markets or [] if m.yes_payout == 1.0), None)
    if won is None:
        return None, ""
    if won.low is None:
        return float(won.high), "bracket"
    if won.high is None:
        return float(won.low), "bracket"
    return (won.low + won.high) / 2, "bracket"


def run(cfg: Config, cases: list[Case], db: DB, model_ids: list[str], csv_path: str | None = None) -> list[dict]:
    """Fit each model's bias on the earlier half of each station/kind, then score and trade the later half
    with every member corrected by its own model's bias.

    Per-model, not pooled: models can err in opposite directions (e.g. GEFS mild and HRRR cold on a
    clear, calm night), and one shared shift can't fix that."""
    bt_cfg = backtest_config(cfg, model_ids)
    groups = defaultdict(list)
    for c in cases:
        if c.markets:
            groups[(c.station.code, c.kind)].append(c)
    rows, all_test = [], []
    for (code, kind), cs in sorted(groups.items()):
        cs.sort(key=lambda c: c.day)
        half = len(cs) // 2
        train, test = cs[:half], cs[half:]
        pooled_errs, model_errs, sources = [], defaultdict(list), set()
        for c in train:
            maxes = member_values(c, db)
            value, source = truth(c)
            if not maxes or value is None:
                continue
            sources.add(source)
            for mid in {m for m, _ in maxes}:
                model_errs[mid].append(float(np.mean([v for (m, _), v in maxes.items() if m == mid])) - value)
            if (raw := score_case(bt_cfg, c, maxes, 0.0)):
                pooled_errs.append(raw.pooled_mean - value)
        model_bias = {m: float(np.mean(e)) for m, e in model_errs.items()}

        def corrected(c):
            return {k: v - model_bias.get(k[0], 0.0) for k, v in member_values(c, db).items()}

        # Spread: raw ensembles are too narrow; pick the widening that scored best on the earlier days.
        fit = {}
        for spread in SPREADS:
            scores = [brier(s.probs, s.hit) for c in train if (s := score_case(bt_cfg, c, corrected(c), 0.0, spread))]
            if scores:
                fit[spread] = float(np.mean(scores))
        spread = min(fit, key=fit.get) if fit else None
        scored = [s for c in test if (s := score_case(bt_cfg, c, corrected(c), 0.0, spread))]
        all_test += scored
        rows.append({"station": code, "kind": kind, "train_days": len(train), "test_days": len(scored),
                     "raw_bias": float(np.mean(pooled_errs)) if pooled_errs else None,
                     "model_bias": model_bias, "spread": spread, "truth": "/".join(sorted(sources)) or "none",
                     "scored": scored})
    simulate(all_test, cfg.paper)
    for row in rows:
        summarize(row)
    if csv_path:
        write_csv(all_test, csv_path)
    return rows


def summarize(row: dict) -> None:
    scored = row["scored"]
    with_market = [s for s in scored if s.market]
    diffs = {s.case.day.isoformat(): [brier(s.probs, s.hit) - brier(s.market, s.hit)] for s in with_market}
    row.update(brier_difference(diffs) or {"brier_diff": None, "brier_diff_se": None, "brier_days": 0})
    row["model_brier"] = float(np.mean([brier(s.probs, s.hit) for s in with_market])) if with_market else None
    row["market_brier"] = float(np.mean([brier(s.market, s.hit) for s in with_market])) if with_market else None
    trades = [t for s in scored for t in s.trades]
    row["trades"] = len(trades)
    row["staked"] = sum(t["cost"] for t in trades)
    row["pnl"] = sum(t["payout"] - t["cost"] for t in trades)
    positions = [{"contracts": t["contracts"], "cost": t["cost"], "payout": t["payout"], "prob": t["prob"]} for t in trades]
    row["luck"] = luck_from_positions(positions) if positions else None


def write_csv(scored: list[Scored], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["station", "kind", "day", "bracket", "model_prob", "market_prob", "won", "trade_side",
                    "contracts", "cost", "payout"])
        for s in scored:
            trades = {t["bracket"]: t for t in s.trades}
            for i, m in enumerate(s.case.markets):
                t = trades.get(m.bracket.label, {})
                w.writerow([s.case.station.code, s.case.kind, s.case.day, m.bracket.label, round(s.probs[i], 4),
                            None if s.market is None else round(s.market[i], 4), int(i == s.hit),
                            t.get("side", ""), t.get("contracts", ""), round(t["cost"], 2) if t else "",
                            round(t["payout"], 2) if t else ""])
