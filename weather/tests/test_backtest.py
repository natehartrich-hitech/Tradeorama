"""Backtest building blocks, offline."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from wxbrackets import backtest as bt
from wxbrackets.archive import MODELS, byte_range_for
from wxbrackets.brackets import Bracket
from wxbrackets.config import PaperSettings
from wxbrackets.polymarket_us import BracketMarket

UTC = timezone.utc


def test_runs_are_only_used_once_published():
    gefs, hrrr = MODELS["gefs"], MODELS["hrrr"]
    decision = datetime(2026, 10, 22, 0, tzinfo=UTC)  # 6 PM Chicago, the evening before
    end = datetime(2026, 10, 23, 8, tzinfo=UTC)
    assert gefs.run_for(decision, end) == datetime(2026, 10, 21, 18, tzinfo=UTC)  # 18Z + 6 h = 00Z: just in
    assert gefs.run_for(decision - timedelta(minutes=1), end) == datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert hrrr.run_for(decision, end) == datetime(2026, 10, 21, 18, tzinfo=UTC)
    assert MODELS["refs"].run_for(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)) is None  # before its archive


def test_gefs_pieces_cover_the_day_without_spilling_outside():
    gefs = MODELS["gefs"]
    init = datetime(2026, 10, 21, 18, tzinfo=UTC)
    start, end = datetime(2026, 10, 22, 6, tzinfo=UTC), datetime(2026, 10, 23, 6, tzinfo=UTC)
    pieces = gefs.pieces(init, 0, start, end, "low")
    snaps = [p for p in pieces if p.start == p.end]
    spans = [p for p in pieces if p.start != p.end]
    assert [p.start.hour for p in snaps] == [6, 9, 12, 15, 18, 21, 0, 3, 6]
    assert all(start <= p.start and p.end <= end for p in spans)
    assert {(p.end - p.start).seconds // 3600 for p in spans} == {3, 6}
    assert all(p.key.startswith("TMIN") for p in spans) and "gec00" in pieces[0].url


def test_ecmwf_index_lookup():
    rows = [{"param": "2t", "number": "7", "step": "24", "_offset": 100, "_length": 50},
            {"param": "mx2t3", "number": "7", "step": "24", "_offset": 150, "_length": 40}]
    text = "\n".join(json.dumps(r) for r in rows)
    assert byte_range_for(text, ("ecmwf", "mx2t3", "7", "24")) == (150, 189)
    assert byte_range_for(text, ("ecmwf", "2t", "8", "24")) is None


def market(slug, low, high, payout, fee=0.0695):
    label = f"{low}-{high}" if low is not None and high is not None else (f"≤{high}" if low is None else f"≥{low}")
    return BracketMarket(slug, Bracket(label, low, high), None, None, fee, "MARKET_STATUS_RESOLVED", payout)


def test_truth_prefers_official_then_bracket_midpoint():
    c = bt.Case(None, "high", date(2026, 10, 1), None, (None, None))
    c.markets = [market("a", None, 63, 0.0), market("b", 64, 65, 1.0), market("c", 66, None, 0.0)]
    assert bt.truth(c) == (64.5, "bracket")
    c.official = 65
    assert bt.truth(c) == (65.0, "official")
    c.official, c.markets = None, [market("a", None, 63, 1.0), market("c", 64, None, 0.0)]
    assert bt.truth(c) == (63.0, "bracket")


def scored(code, day, probs, prices, payouts):
    markets = [market(f"{code}{i}", 60 + 2 * i, 61 + 2 * i, p) for i, p in enumerate(payouts)]
    case = bt.Case(SimpleNamespace(code=code), "high", day, None, (None, None), markets=markets,
                   prices={m.slug: pr for m, pr in zip(markets, prices)})
    return bt.Scored(case, probs, None, payouts.index(1.0), 0.0)


def test_simulated_trades_follow_the_paper_rules():
    day = date(2026, 10, 1)
    # Model 60% on bracket 0, offered at 30c: a big edge, so Kelly wants more than the $25 event cap.
    a = scored("CHI", day, [0.6, 0.4], [(0.30, 0.72), (0.70, 0.31)], [1.0, 0.0])
    b = scored("NYC", day, [0.5, 0.5], [(0.49, 0.53), (0.49, 0.53)], [0.0, 1.0])  # no edge
    bt.simulate([a, b], PaperSettings())
    assert not b.trades
    spent = sum(t["cost"] for t in a.trades)
    assert 24 <= spent <= 25.01  # capped per event
    yes = next(t for t in a.trades if t["side"] == "yes")
    assert yes["payout"] == pytest.approx(yes["contracts"])  # bracket 0 won


def test_daily_cap_across_cities():
    day = date(2026, 10, 1)
    many = [scored(c, day, [0.7, 0.3], [(0.30, 0.72), (0.70, 0.31)], [1.0, 0.0]) for c in ("A", "B", "C", "D", "E")]
    bt.simulate(many, PaperSettings())
    assert sum(t["cost"] for s in many for t in s.trades) <= 100.01


def test_land_mask_moves_a_station_off_the_water():
    from test_rrfs import eccodes, make_grib

    def grid(values):
        gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        for key, val in [("Ni", 3), ("Nj", 3), ("latitudeOfFirstGridPointInDegrees", 43.0),
                         ("longitudeOfFirstGridPointInDegrees", 271.0), ("latitudeOfLastGridPointInDegrees", 41.0),
                         ("longitudeOfLastGridPointInDegrees", 273.0), ("iDirectionIncrementInDegrees", 1.0),
                         ("jDirectionIncrementInDegrees", 1.0)]:
            eccodes.codes_set(gid, key, val)
        eccodes.codes_set_values(gid, values)
        msg = eccodes.codes_get_message(gid)
        eccodes.codes_release(gid)
        return msg

    from wxbrackets.rrfs import GribPoint
    # Rows north to south (43, 42, 41), columns 271, 272, 273. The point nearest 42.1N 272.1E is the
    # centre (42, 272): make it water, and give it a different temperature.
    temps = grid([280, 280, 280, 280, 290, 280, 280, 280, 280])
    land = grid([1, 1, 1, 1, 0, 1, 1, 1, 1])
    assert GribPoint().value(temps, 42.1, 272.1 - 360) == pytest.approx(290)  # plain nearest: the water point
    gp = GribPoint()
    gp.set_mask(land)
    assert gp.value(temps, 42.1, 272.1 - 360) == pytest.approx(280)  # nearest land point


def test_download_holds_few_messages_in_memory_and_saves_everything(tmp_path, monkeypatch):
    import threading

    from wxbrackets.archive import Piece
    from wxbrackets.db import DB

    t = datetime(2026, 10, 21, tzinfo=UTC)
    pieces = {}
    for f in range(400):  # 400 files, two messages each
        url = f"https://noaa-gefs-pds.s3.amazonaws.com/f{f:03d}"
        for fld in ("TMP:2 m above ground", "TMAX:2 m above ground"):
            pieces[(url, fld)] = Piece(url, fld, ("wgrib2", fld), t, t)
    monkeypatch.setattr(bt, "unique_pieces", lambda cases: pieces)
    monkeypatch.setattr(bt, "MASKS", {})
    idx = "1:0:d=2026102100:TMP:2 m above ground:3 hour fcst:\n2:100:d=2026102100:TMAX:2 m above ground:0-3 hour max fcst:\n"
    held = {"now": 0, "max": 0}
    lock = threading.Lock()

    class Client:
        def get(self, url, headers=None):
            if url.endswith(".idx"):
                return SimpleNamespace(status_code=200, text=idx)
            with lock:
                held["now"] += 1
                held["max"] = max(held["max"], held["now"])
            return SimpleNamespace(status_code=206, content=b"x" * 10)

    class Grib:
        def set_mask(self, message):
            pass

        def values(self, data, points):
            with lock:
                held["now"] -= 1
            return [273.15] * len(points)

    monkeypatch.setattr(bt, "GribPoint", Grib)
    db = DB(tmp_path / "bt.db")
    stations = [SimpleNamespace(lat=41.8, lon=-87.8), SimpleNamespace(lat=40.8, lon=-74.0)]
    assert bt.download([], db, stations, Client(), workers=4, progress=lambda s: None) == 800
    assert held["max"] <= 4 * 2 * 2  # in-flight tasks x messages per task, not the whole run
    assert db.conn.execute("SELECT COUNT(*) FROM grib_points").fetchone()[0] == 800 * 2
    assert db.get_point("https://noaa-gefs-pds.s3.amazonaws.com/f399", "TMAX:2 m above ground", 40.8, -74.0) == pytest.approx(32.0)
