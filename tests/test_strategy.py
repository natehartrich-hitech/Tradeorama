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
    # Fee per share = rate * p * (1 - p): 0.05 * 0.55 * 0.45 = 1.2375 cents.
    assert edge(0.60, 0.55, fee_rate=0.05) == pytest.approx(0.05 - 0.012375)
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
        book=lambda token: books[token], fee_rate=lambda token: 0, clock=NOW.timestamp,
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


# --- polling schedule -----------------------------------------------------------------

from tradeorama.strategy.bot import poll_delay  # noqa: E402


@pytest.mark.parametrize("hours_to_game,expected_min", [
    (None, 360),   # no games listed: every 6 hours
    (1, 5),        # within 2 hours: every 5 minutes
    (2.1, 6),      # just outside 2 hours: wake when it crosses into the fast tier
    (6, 15),       # 2-12 hours: every 15 minutes
    (12.5, 30),    # wake when it crosses into the 12-hour tier
    (30, 60),      # far out: hourly
])
def test_poll_delay_tiers(tmp_path, hours_to_game, expected_min):
    s = strat(tmp_path)
    now = NOW.timestamp()
    start = None if hours_to_game is None else now + hours_to_game * 3600
    assert poll_delay(start, now, 300, s) == pytest.approx(expected_min * 60)


def test_cycle_tracks_next_game_start(tmp_path):
    bot = make_bot(tmp_path, {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)})
    bot.cycle(execute=False)
    assert bot.next_start["basketball_nba"] == TIP.timestamp()


def test_nightly_slate_uses_far_fewer_calls(tmp_path):
    """One NBA-style day: games at 7pm, 7:30pm, 8pm, 10pm and 10:30pm ET. Count odds fetches over 24h."""
    s = strat(tmp_path)
    day = datetime(2026, 11, 5, 4, 0, tzinfo=timezone.utc).timestamp()  # midnight ET
    starts = [day + h * 3600 for h in (19, 19.5, 20, 22, 22.5)] + [day + (24 + 19) * 3600]
    t, calls = day, 0
    while t < day + 24 * 3600:
        calls += 1
        upcoming = [x for x in starts if x > t + 5 * 60]
        t += poll_delay(min(upcoming), t, 300, s)
    assert calls < 288 / 2  # fixed 5-minute polling would be 288


def test_run_loop_single_cycle(tmp_path):
    bot = make_bot(tmp_path, {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)})
    bot.run(interval=300, max_cycles=1)
    assert bot.ledger.summary("paper")["trades"] == 1


def test_summary_by_sport(tmp_path, monkeypatch):
    ledger = Ledger(str(tmp_path / "s.db"))
    start = NOW.timestamp()
    common = dict(ts=start - 3600, mode="paper", commence=start - 10, market_id="m", label="x", question="q")
    ledger.record_fill(sport="basketball_nba", event_id="a", token_id="nba1", size=10, cost=5.0, fair=0.55, edge=0.05, **common)
    ledger.record_fill(sport="basketball_nba", event_id="b", token_id="nba2", size=10, cost=4.0, fair=0.45, edge=0.05, **common)
    ledger.record_fill(sport="mma_mixed_martial_arts", event_id="c", token_id="ufc1", size=10, cost=6.0, fair=0.64, edge=0.04, **common)
    ledger.record_closing("nba1", 0.58, commence=start - 10, now=start - 20)  # +8¢
    ledger.record_closing("ufc1", 0.57, commence=start - 10, now=start - 20)  # -3¢
    for f in ledger.unsettled(now=start):
        ledger.settle(f["id"], 10.0 if f["token_id"] == "nba1" else 0.0)

    monkeypatch.setattr("time.time", lambda: start)  # CLV only counts once the game has started
    by = ledger.summary_by_sport("paper")
    assert list(by) == ["basketball_nba", "mma_mixed_martial_arts"]
    nba, ufc = by["basketball_nba"], by["mma_mixed_martial_arts"]
    assert nba["trades"] == 2 and nba["clv_trades"] == 1 and nba["avg_clv_cents"] == pytest.approx(8.0)
    assert nba["settled_pnl_usd"] == pytest.approx(1.0) and nba["roi_pct"] == pytest.approx(11.1)
    assert ufc["avg_clv_cents"] == pytest.approx(-3.0) and ufc["roi_pct"] == pytest.approx(-100.0)


def test_strategy_api_includes_sports(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tradeorama.app import create_app
    from test_app import FakeMarkets

    db = tmp_path / "api.db"
    Ledger(str(db)).record_fill(venue="polymarket-us", mode="paper", sport="icehockey_nhl", event_id="e", commence=0, market_id="m",
                                token_id="t", label="Bruins", question="q", size=10, cost=5.0, fair=0.55, edge=0.05)
    monkeypatch.setenv("STRATEGY_DB", str(db))
    settings = make_settings()
    body = TestClient(create_app(settings, Trader(settings), FakeMarkets())).get("/api/strategy").json()
    assert [(r["mode"], r["sport"], r["trades"]) for r in body["by_sport"]] == [("paper", "icehockey_nhl", 1)]


def test_fee_matches_polymarket_formula():
    from tradeorama.strategy.ev import fee_per_share

    # Largest at 50 cents, symmetric, zero at the extremes.
    assert fee_per_share(0.50, 0.05) == pytest.approx(0.0125)
    assert fee_per_share(0.33, 0.05) == pytest.approx(fee_per_share(0.67, 0.05))
    assert fee_per_share(0.99, 0.05) < 0.0005


def test_bot_uses_configured_fee_rate(tmp_path):
    s = strat(tmp_path, taker_fee_rate=0.05)
    settings = make_settings()
    events = [{"markets": [market("Celtics vs. Lakers", ["Celtics", "Lakers"], tokens=("celtics", "lakers"))]}]
    books = {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)}
    bot = Bot(settings, s, FakeOdds([nba_line()]), FakeSports(events), Trader(settings), Ledger(s.db_path),
              book=lambda t: books[t], clock=NOW.timestamp)
    lakers = {d.leg.label: d for d in bot.cycle(execute=False)}["Lakers"]
    assert lakers.edge == pytest.approx(0.06 - 0.05 * 0.54 * 0.46)


# --- diagnosis and paging ---------------------------------------------------------------

from tradeorama.strategy.matching import SportsMarkets, candidates, why_unmatched  # noqa: E402


def test_why_unmatched_explains_each_rejection():
    line = nba_line()
    ev = {}
    assert why_unmatched(market("Celtics vs. Lakers", ["Celtics", "Lakers"]), ev, line) == "matched"
    no_time = market("Celtics vs. Lakers", ["Celtics", "Lakers"])
    no_time.pop("gameStartTime")
    assert why_unmatched(no_time, ev, line).startswith("no game start time")
    late = market("Celtics vs. Lakers", ["Celtics", "Lakers"], start=TIP + timedelta(days=1))
    assert "from Pinnacle's" in why_unmatched(late, ev, line)
    assert "spread" in why_unmatched(market("Spread: Lakers (-4.5)", ["Lakers", "Celtics"]), ev, line)
    assert "don't map" in why_unmatched(market("Celtics vs. Knicks", ["Celtics", "Knicks"]), ev, line)


def test_candidates_find_related_markets_loosely():
    events = [{"title": "Celtics vs. Lakers", "markets": [market("Celtics vs. Lakers", ["Celtics", "Lakers"])]},
              {"title": "Nuggets vs. Jazz", "markets": [market("Nuggets vs. Jazz", ["Nuggets", "Jazz"], id="m2")]}]
    assert [m["question"] for m, _ in candidates(nba_line(), events)] == ["Celtics vs. Lakers"]


def test_events_pages_through_all_results():
    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Http:
        def __init__(self):
            self.offsets = []

        def get(self, url, params):
            # Like Gamma: never more than 100 per page, however many are asked for.
            self.offsets.append(params["offset"])
            return Resp([{"id": i} for i in range(params["offset"], min(params["offset"] + 100, 250))])

    http = Http()
    events = SportsMarkets(http).events("americanfootball_nfl")
    assert len(events) == 250 and http.offsets == [0, 100, 200, 250]


def test_cli_does_not_log_request_urls(tmp_path, monkeypatch, caplog):
    import logging
    from tradeorama.strategy import __main__ as cli

    monkeypatch.setattr(cli, "cmd_report", lambda args: logging.getLogger("httpx").info("GET ...?apiKey=secret"))
    monkeypatch.setenv("STRATEGY_DB", str(tmp_path / "x.db"))
    with caplog.at_level(logging.INFO):
        cli.main(["report"])
    assert "secret" not in caplog.text


def test_kelly_is_a_target_position_not_a_per_cycle_amount(tmp_path):
    # Small bankroll so Kelly, not the $25 game cap, sets the size.
    books = {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)}
    bot = make_bot(tmp_path, books, bankroll_usd=100)
    first = {d.leg.label: d for d in bot.cycle()}["Lakers"]
    target = kelly_fraction(0.6, 0.54) * 0.25 * 100
    assert first.action == "buy" and first.cost == pytest.approx(target, abs=0.02)

    # Same edge five minutes later: already at the target, so nothing more.
    again = {d.leg.label: d for d in bot.cycle()}["Lakers"]
    assert again.action == "skip" and "already holding its target" in again.reason

    # The edge grows: top up only the difference.
    bot.odds = FakeOdds([nba_line(home=0.66, away=0.34)])
    more = {d.leg.label: d for d in bot.cycle()}["Lakers"]
    bigger = kelly_fraction(0.66, 0.54) * 0.25 * 100
    assert more.action == "buy" and more.cost == pytest.approx(bigger - first.cost, abs=0.02)
    assert bot.ledger.position_cost("paper", "lakers", "polymarket") == pytest.approx(bigger, abs=0.03)
