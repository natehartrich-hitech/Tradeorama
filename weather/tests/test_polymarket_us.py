"""Polymarket US weather markets, on real gateway.polymarket.us responses (November 2026)."""

from datetime import datetime, timezone

import httpx
import pytest

from conftest import fixture_json
from wxbrackets import paper
from wxbrackets.brackets import Bracket
from wxbrackets.db import DB
from wxbrackets.http import Http
from wxbrackets.polymarket_us import PolymarketUS, book_side, check_tiling, parse_bracket, parse_event

EVENT = "pmus_temp_mdwhigh_2026-11-04.json"
RESOLVED = "pmus_temp_mdwhigh_2026-11-02_resolved.json"


@pytest.mark.parametrize("title,expected", [
    ("63 or below", Bracket("≤63", None, 63)),
    ("64 to 65", Bracket("64-65", 64, 65)),
    ("72 or above", Bracket("≥72", 72, None)),
    ("72°F or above", Bracket("≥72", 72, None)),
    ("Between 64 and 65", None),
])
def test_parse_bracket(title, expected):
    assert parse_bracket(title) == expected


def test_parse_real_event():
    ms = parse_event(fixture_json(EVENT)["event"])
    assert [m.bracket.label for m in ms] == ["≤63", "64-65", "66-67", "68-69", "70-71", "≥72"]
    first = ms[0]
    assert (first.slug, first.yes_bid, first.yes_ask, first.no_ask, first.fee_rate) == (
        "tc-temp-mdwhigh-2026-11-04-lt64f", 0.05, 0.09, 0.95, 0.0695)
    assert ms[-1].yes_bid is None and ms[-1].no_ask is None  # nobody bidding "72 or above"


def test_parse_resolved_event():
    ms = parse_event(fixture_json(RESOLVED)["event"])
    assert [m.yes_payout for m in ms] == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_brackets_must_tile():
    with pytest.raises(ValueError, match="gap"):
        check_tiling([Bracket("≤63", None, 63), Bracket("65-66", 65, 66), Bracket("≥67", 67, None)])
    with pytest.raises(ValueError):
        check_tiling([Bracket("64-65", 64, 65), Bracket("≥66", 66, None)])


def test_book_side_on_real_book():
    book = fixture_json("pmus_book_mdw_64to65.json")
    assert book_side(book, "yes")[0] == (0.43, 27.0)
    assert book_side(book, "no")[0] == (0.58, 1.03)  # 1 - the best 0.42 bid


def test_fee_and_edge():
    assert paper.fee_per_contract(0.5, 0.0695) == pytest.approx(0.017375)
    assert paper.edge(0.45, 0.33, 0.0695) == pytest.approx(0.12 - 0.0695 * 0.33 * 0.67)


def test_plan_buy_stops_where_edge_runs_out():
    fill = paper.plan_buy(0.45, [(0.33, 10), (0.36, 10), (0.42, 100)], budget=1000, min_edge=0.03, fee_rate=0.0695)
    assert fill.contracts == 20  # 0.42 leaves under 3¢ after the fee
    small = paper.plan_buy(0.45, [(0.33, 100)], budget=5, min_edge=0.03, fee_rate=0.0695)
    assert small.cost == pytest.approx(5, abs=0.01) and small.contracts == pytest.approx(5 / (0.33 + 0.0695 * 0.33 * 0.67), abs=0.01)


def fake_gateway(resolved=False):
    def handler(request):
        path = request.url.path
        if path.startswith("/v1/events/slug/"):
            return httpx.Response(200, json=fixture_json(EVENT))
        if path.endswith("/book"):
            return httpx.Response(200, json=fixture_json("pmus_book_mdw_64to65.json"))
        if path.startswith("/v1/market/slug/"):
            slug = path.rsplit("/", 1)[1]
            win = slug.endswith("gte64lt65f")
            status = "MARKET_STATUS_RESOLVED" if resolved else "MARKET_STATUS_OPEN"
            return httpx.Response(200, json={"market": {"slug": slug, "status": status,
                                                        "outcomePrices": '["1","0"]' if win else '["0","1"]'}})
        return httpx.Response(404)
    http = Http("t", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    return PolymarketUS(http, "https://gw.test")


def test_trade_respects_edge_caps_and_settles(cfg, tmp_path):
    db = DB(tmp_path / "p.db")
    pmus = fake_gateway()
    markets = pmus.markets("temp-mdwhigh", datetime(2026, 11, 4).date())
    probs = [0.05, 0.60, 0.30, 0.04, 0.01, 0.0]  # model likes 64-65 (ask 43c in the saved book)
    now = datetime(2026, 11, 3, 20, tzinfo=timezone.utc)
    opps = paper.trade(db, pmus, cfg.paper, "CHI", "2026-11-04", None, markets, probs, hours_left=30, now=now)
    buys = [o for o in opps if o.action == "buy"]
    assert [(o.side, o.market.bracket.label) for o in buys][0] == ("yes", "64-65")
    assert db.paper_spent(station="CHI", target_date="2026-11-04") <= cfg.paper.max_event_usd + 1e-9

    # Same again: the per-event cap is used up or the remaining budget is tiny.
    again = paper.trade(db, pmus, cfg.paper, "CHI", "2026-11-04", None, markets, probs, hours_left=30, now=now)
    assert db.paper_spent(station="CHI", target_date="2026-11-04") <= cfg.paper.max_event_usd + 1e-9
    assert all(o.action == "skip" for o in again) or sum(o.fill.cost for o in again if o.fill) < 25

    # Too close to the end of the climate day: nothing.
    late = paper.trade(db, pmus, cfg.paper, "CHI", "2026-11-05", None, markets, probs, hours_left=1, now=now)
    assert not any(o.action == "buy" for o in late)

    assert paper.settle(db, pmus) == 0  # not resolved yet
    assert paper.settle(db, fake_gateway(resolved=True)) >= 1
    s = paper.summary(db, "CHI")
    assert s["settled"] == s["trades"] and s["settled_pnl_usd"] > 0  # 64-65 won
