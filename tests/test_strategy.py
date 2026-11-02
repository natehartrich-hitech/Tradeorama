from datetime import datetime, timedelta, timezone

import pytest

from tradeorama.config import StrategySettings
from tradeorama.strategy.bot import Bot
from tradeorama.strategy.ev import edge, kelly_fraction, plan_buy
from tradeorama.strategy.ledger import Ledger
from tradeorama.strategy.matching import legs_for_market, match, same_team
from tradeorama.strategy.odds import Line, devig, parse_events
from tradeorama.trader import Trader

from test_app import make_settings

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
TIP = NOW + timedelta(hours=6)


def nba_line(home=0.6, away=0.4, updated=NOW):
    return Line("ev1", "basketball_nba", "Los Angeles Lakers", "Boston Celtics", TIP, updated,
                {"Los Angeles Lakers": home, "Boston Celtics": away})


def soccer_line():
    return Line("ev2", "soccer_epl", "Arsenal", "Chelsea", TIP, NOW, {"Arsenal": 0.5, "Chelsea": 0.25, "Draw": 0.25})


def market(question, outcomes, tokens=("t1", "t2"), start=TIP, **extra):
    return {
        "id": extra.pop("id", "m1"),
        "question": question,
        "outcomes": str(list(outcomes)).replace("'", '"'),
        "clobTokenIds": str(list(tokens)).replace("'", '"'),
        "gameStartTime": start.strftime("%Y-%m-%d %H:%M:%S+00"),
        **extra,
    }


# --- odds -------------------------------------------------------------------------


def test_devig_removes_margin():
    fair = devig({"A": 1.80, "B": 2.10})
    assert sum(fair.values()) == pytest.approx(1)
    assert fair["A"] == pytest.approx(0.5385, abs=1e-4)


def test_parse_events_reads_pinnacle_h2h():
    events = [{
        "id": "x", "sport_key": "basketball_nba", "commence_time": "2026-11-06T00:00:00Z",
        "home_team": "Los Angeles Lakers", "away_team": "Boston Celtics",
        "bookmakers": [{"key": "pinnacle", "last_update": "2026-11-05T17:59:00Z", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Los Angeles Lakers", "price": 1.6}, {"name": "Boston Celtics", "price": 2.5}]}
        ]}],
    }]
    [line] = parse_events(events)
    assert line.commence == datetime(2026, 11, 6, tzinfo=timezone.utc)
    assert line.fair["Los Angeles Lakers"] > 0.6


# --- ev math ------------------------------------------------------------------------


def test_edge_and_kelly():
    assert edge(0.60, 0.55) == pytest.approx(0.05)
    assert edge(0.60, 0.55, fee_rate_bps=100) == pytest.approx(0.05 - 0.01 * 0.45)
    assert kelly_fraction(0.60, 0.55) == pytest.approx(0.05 / 0.45)
    assert kelly_fraction(0.50, 0.55) == 0


def test_plan_buy_stops_at_edge_threshold_and_budget():
    asks = [(0.57, 100), (0.55, 10), (0.56, 20)]
    fill = plan_buy(0.60, asks, budget=1000, min_edge=0.035)
    # Only 0.55 and 0.56 clear a 3.5¢ edge.
    assert fill.size == 30 and fill.limit_price == 0.56
    small = plan_buy(0.60, asks, budget=2.75, min_edge=0.01)
    assert small.size == 5 and small.cost == pytest.approx(2.75)
    assert plan_buy(0.60, asks, budget=100, min_edge=0.06) is None


# --- matching -----------------------------------------------------------------------


@pytest.mark.parametrize("poly,odds,ok", [
    ("Lakers", "Los Angeles Lakers", True),
    ("Arsenal FC", "Arsenal", True),
    ("Los Angeles", "Los Angeles Lakers", True),
    ("Man City", "Manchester City", False),
    ("Celtics", "Los Angeles Lakers", False),
])
def test_same_team(poly, odds, ok):
    assert same_team(poly, odds) is ok


def test_team_vs_team_moneyline_maps_both_sides():
    legs = legs_for_market(market("Celtics vs. Lakers", ["Celtics", "Lakers"]), nba_line())
    assert legs == [("t1", 0.4, "Celtics"), ("t2", 0.6, "Lakers")]


@pytest.mark.parametrize("question,extra", [
    ("Spread: Lakers (-4.5)", {}),
    ("Lakers vs. Celtics: O/U 224.5", {}),
    ("Lakers vs. Celtics 1H Moneyline", {}),
    ("Lakers vs. Celtics", {"sportsMarketType": "spreads"}),
])
def test_non_moneyline_markets_are_skipped(question, extra):
    assert legs_for_market(market(question, ["Lakers", "Celtics"], **extra), nba_line()) == []


def test_soccer_yes_no_markets():
    line = soccer_line()
    win = legs_for_market(market("Will Arsenal FC win on 2026-11-05?", ["Yes", "No"]), line)
    assert [round(p, 2) for _, p, _ in win] == [0.5, 0.5]
    draw = legs_for_market(market("Will Arsenal FC vs. Chelsea FC end in a draw?", ["Yes", "No"]), line)
    assert [p for _, p, _ in draw] == [0.25, 0.75]
    # Two-way team market can't be priced from a three-way line.
    assert legs_for_market(market("Arsenal vs. Chelsea", ["Arsenal", "Chelsea"]), line) == []


def test_match_requires_close_start_time():
    events = [{"markets": [market("Celtics vs. Lakers", ["Celtics", "Lakers"], start=TIP + timedelta(days=1))]}]
    assert match(nba_line(), events) == []
    events = [{"markets": [market("Celtics vs. Lakers", ["Celtics", "Lakers"])]}]
    assert len(match(nba_line(), events)) == 2


# --- bot ----------------------------------------------------------------------------


class FakeOdds:
    credits_remaining = "100"

    def __init__(self, lines):
        self._lines = lines

    def lines(self, sport):
        return self._lines


class FakeSports:
    def __init__(self, events):
        self._events = events

    def events(self, sport):
        return self._events


def strat(tmp_path, **kw):
    base = dict(odds_api_key="k", sports=["basketball_nba"], bankroll_usd=1000, kelly_multiplier=0.25,
                min_edge=0.03, max_game_exposure_usd=25, max_daily_spend_usd=100, max_odds_age_sec=300,
                min_minutes_before_start=5, max_divergence=0.15, db_path=str(tmp_path / "t.db"))
    base.update(kw)
    return StrategySettings(**base)


def make_bot(tmp_path, books, line=None, settings=None, **kw):
    settings = settings or make_settings()
    s = strat(tmp_path, **kw)
    events = [{"markets": [market("Celtics vs. Lakers", ["Celtics", "Lakers"], tokens=("celtics", "lakers"))]}]
    return Bot(
        settings, s, FakeOdds([line or nba_line()]), FakeSports(events), Trader(settings), Ledger(s.db_path),
        book=lambda token: books[token], fee_rate_bps=lambda token: 0, clock=NOW.timestamp,
    )


def book(bid, ask, size=1000):
    return {"bids": [{"price": str(bid), "size": str(size)}], "asks": [{"price": str(ask), "size": str(size)}], "min_order_size": "5"}


def test_bot_buys_underpriced_side_and_respects_game_cap(tmp_path):
    bot = make_bot(tmp_path, {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)})
    decisions = {d.leg.label: d for d in bot.cycle()}
    assert decisions["Celtics"].action == "skip"
    buy = decisions["Lakers"]
    assert buy.action == "buy"
    assert buy.cost == pytest.approx(25, abs=0.6)  # capped by MAX_GAME_EXPOSURE_USD, under Kelly and MAX_ORDER_USD
    # Second cycle: game cap already used up.
    again = {d.leg.label: d for d in bot.cycle()}["Lakers"]
    assert again.action == "skip" and "risk limit" in again.reason
    s = bot.ledger.summary("paper")
    assert s["trades"] == 1 and s["avg_edge_cents"] == pytest.approx(6.0)


def test_bot_skips_stale_odds_and_suspicious_gaps(tmp_path):
    stale = make_bot(tmp_path, {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)},
                     line=nba_line(updated=NOW - timedelta(minutes=10)))
    assert all(d.reason == "Pinnacle price is stale" for d in stale.cycle())
    weird = make_bot(tmp_path, {"lakers": book(0.20, 0.22), "celtics": book(0.78, 0.80)})
    d = {d.leg.label: d for d in weird.cycle()}["Lakers"]
    assert d.action == "skip" and "disagree" in d.reason


def test_scan_does_not_trade(tmp_path):
    bot = make_bot(tmp_path, {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)})
    assert any(d.action == "buy" for d in bot.cycle(execute=False))
    assert bot.ledger.summary()["trades"] == 0


def test_live_requires_dry_run_off(tmp_path):
    with pytest.raises(RuntimeError, match="DRY_RUN"):
        Bot(
            make_settings(), strat(tmp_path), FakeOdds([]), FakeSports([]), Trader(make_settings()),
            Ledger(str(tmp_path / "x.db")), live=True)


def test_live_order_uses_fak_and_records_actual_fill(tmp_path, monkeypatch):
    settings = make_settings(dry_run=False)
    bot = make_bot(tmp_path, {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)}, settings=settings)
    bot.mode = "live"
    placed = {}

    def fake_place(token, side, price, size, order_type):
        placed.update(token=token, side=side, price=price, size=size, type=order_type)
        return {"response": {"success": True, "orderID": "o1", "makingAmount": "10.8", "takingAmount": "20"}}

    monkeypatch.setattr(bot.trader, "place_limit_order", fake_place)
    buy = {d.leg.label: d for d in bot.cycle()}["Lakers"]
    assert placed["type"] == "FAK" and placed["side"] == "BUY" and placed["price"] == 0.54
    assert buy.size == 20 and buy.cost == 10.8
    [t] = bot.ledger.trades("live")
    assert t["order_id"] == "o1"


def test_clv_and_settlement(tmp_path):
    ledger = Ledger(str(tmp_path / "c.db"))
    start = NOW.timestamp()
    ledger.record_fill(ts=start - 3600, mode="paper", sport="nba", event_id="e", commence=start - 10, market_id="m",
                       token_id="tok", label="Lakers", question="q", size=10, cost=5.0, fair=0.55, edge=0.05)
    ledger.record_closing("tok", 0.58, commence=start - 10, now=start - 20)
    [f] = ledger.unsettled(now=start)
    ledger.settle(f["id"], 10.0)
    [t] = ledger.trades(now=start)
    assert t["clv"] == pytest.approx(0.08)
    assert t["pnl"] == pytest.approx(5.0)
