"""Polymarket US venue, tested on real gateway.polymarket.us responses (November 2026)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from tradeorama.config import StrategySettings
from tradeorama.strategy.bot import Bot
from tradeorama.strategy.ledger import Ledger
from tradeorama.strategy.odds import Line
from tradeorama.strategy.pmus import PolymarketUS, match, side_book
from tradeorama.trader import Trader

from test_app import make_settings

FIX = Path(__file__).parent / "fixtures"
UTC = timezone.utc


def load(name):
    return json.loads((FIX / name).read_text())


def line(home, away, start, fair, event_id="g1", updated=None):
    return Line(event_id, "x", home, away, start, updated or start, fair)


NBA_START = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def test_nba_moneyline_maps_both_teams():
    events = load("pmus_nba_events.json")["events"]
    l = line("Detroit Pistons", "Philadelphia 76ers", NBA_START, {"Detroit Pistons": 0.55, "Philadelphia 76ers": 0.45})
    legs = match(l, events)
    assert [(g.token_id, g.label, g.fair) for g in legs] == [
        ("aec-nba-phi-det-2026-11-04:yes", "76ers", 0.45),  # the long side is the 76ers
        ("aec-nba-phi-det-2026-11-04:no", "Pistons", 0.55),
    ]
    assert legs[0].fee_rate == 0.0695


def test_wrong_start_time_or_teams_do_not_match():
    events = load("pmus_nba_events.json")["events"]
    fair = {"Detroit Pistons": 0.55, "Philadelphia 76ers": 0.45}
    assert match(line("Detroit Pistons", "Philadelphia 76ers", datetime(2026, 11, 6, 0, tzinfo=UTC), fair), events) == []
    assert match(line("Boston Celtics", "Philadelphia 76ers", NBA_START, {"Boston Celtics": 0.5, "Philadelphia 76ers": 0.5}), events) == []


def test_nhl_short_team_names():
    events = load("pmus_nhl_events.json")["events"]
    l = line("Philadelphia Flyers", "Minnesota Wild", datetime(2026, 11, 4, 0, tzinfo=UTC),
             {"Philadelphia Flyers": 0.52, "Minnesota Wild": 0.48})
    legs = match(l, events)
    assert [(g.label, g.fair) for g in legs] == [("Wild", 0.48), ("Flyers", 0.52)]


def test_soccer_home_draw_away():
    events = load("pmus_epl_events.json")["events"]
    l = line("Everton", "Coventry City", datetime(2026, 11, 6, 20, tzinfo=UTC),
             {"Everton": 0.557, "Coventry City": 0.204, "Draw": 0.239})
    legs = {g.token_id: g.fair for g in match(l, events)}
    assert legs == pytest.approx({
        "atc-epl-eve-cov-2026-11-06-eve:yes": 0.557, "atc-epl-eve-cov-2026-11-06-eve:no": 0.443,
        "atc-epl-eve-cov-2026-11-06-draw:yes": 0.239, "atc-epl-eve-cov-2026-11-06-draw:no": 0.761,
        "atc-epl-eve-cov-2026-11-06-cov:yes": 0.204, "atc-epl-eve-cov-2026-11-06-cov:no": 0.796,
    })


def test_side_book_buying_no_hits_yes_bids():
    book = load("pmus_book.json")
    yes, no = side_book(book, "yes"), side_book(book, "no")
    assert yes["asks"][0] == {"price": 0.43, "size": 27.0}
    assert no["asks"][0] == {"price": 0.58, "size": 1.03}  # 1 - 0.42 bid
    assert no["bids"][0] == {"price": 0.57, "size": 27.0}  # 1 - 0.43 offer


def fake_gateway(books: dict, resolved: dict | None = None):
    def handler(request):
        path = request.url.path
        if path.startswith("/v2/leagues/nba/events"):
            events = load("pmus_nba_events.json")["events"] if request.url.params["offset"] == "0" else []
            return httpx.Response(200, json={"events": events})
        if path.endswith("/book"):
            slug = path.split("/")[3]
            return httpx.Response(200, json=books[slug])
        if path.startswith("/v1/market/slug/"):
            slug = path.rsplit("/", 1)[1]
            status, prices = (resolved or {}).get(slug, ("MARKET_STATUS_OPEN", '["0.5","0.5"]'))
            return httpx.Response(200, json={"market": {"slug": slug, "status": status, "outcomePrices": prices}})
        return httpx.Response(404)
    return PolymarketUS(httpx.Client(transport=httpx.MockTransport(handler)), base_url="https://gw.test")


def book_json(bid, ask, qty=500):
    return {"marketData": {"bids": [{"px": {"value": str(bid)}, "qty": str(qty)}],
                           "offers": [{"px": {"value": str(ask)}, "qty": str(qty)}]}}


class Odds:
    credits_remaining = "1"

    def __init__(self, lines):
        self._lines = lines

    def lines(self, sport):
        return self._lines


def strat(tmp_path):
    return StrategySettings(odds_api_key="k", sports=["basketball_nba"], bankroll_usd=1000, kelly_multiplier=0.25,
                            min_edge=0.03, max_game_exposure_usd=25, max_daily_spend_usd=100, max_odds_age_sec=300,
                            min_minutes_before_start=5, max_divergence=0.15, db_path=str(tmp_path / "t.db"))


def test_bot_paper_trades_on_polymarket_us_and_settles(tmp_path):
    slug = "aec-nba-phi-det-2026-11-04"
    now = datetime(2026, 11, 4, 20, tzinfo=UTC)
    # Pinnacle: Pistons 60%. Polymarket US: 76ers bid 0.46 / ask 0.48, so Pistons cost 1 - 0.46 = 0.54.
    l = line("Detroit Pistons", "Philadelphia 76ers", NBA_START, {"Detroit Pistons": 0.60, "Philadelphia 76ers": 0.40}, updated=now)
    venue = fake_gateway({slug: book_json(0.46, 0.48)})
    s = strat(tmp_path)
    bot = Bot(make_settings(), s, Odds([l]), venue, Trader(make_settings()), Ledger(s.db_path), clock=now.timestamp)
    decisions = {d.leg.label: d for d in bot.cycle()}
    assert decisions["76ers"].action == "skip"
    pistons = decisions["Pistons"]
    assert pistons.action == "buy" and pistons.best_ask == pytest.approx(0.54)
    # Edge after Polymarket US's fee: 0.60 - 0.54 - 0.0695 * 0.54 * 0.46
    assert pistons.edge == pytest.approx(0.06 - 0.0695 * 0.54 * 0.46)
    [t] = bot.ledger.trades(venue="polymarket-us")
    assert t["token_id"] == f"{slug}:no" and t["venue"] == "polymarket-us"

    # After the game: Pistons won, so the short side pays 1.
    bot.venue = fake_gateway({}, {slug: ("MARKET_STATUS_RESOLVED", '["0","1"]')})
    bot.clock = lambda: NBA_START.timestamp() + 4 * 3600
    assert bot.settle() == 1
    [t] = bot.ledger.trades(venue="polymarket-us", now=bot.clock())
    assert t["payout"] == pytest.approx(t["size"])


def test_live_trading_is_refused_on_polymarket_us(tmp_path):
    s = strat(tmp_path)
    with pytest.raises(RuntimeError, match="isn't supported on polymarket-us"):
        Bot(make_settings(dry_run=False), s, Odds([]), fake_gateway({}), Trader(make_settings()), Ledger(s.db_path), live=True)


def test_old_databases_get_a_venue_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE fills (id INTEGER PRIMARY KEY, ts REAL NOT NULL, mode TEXT NOT NULL, sport TEXT NOT NULL,
        event_id TEXT NOT NULL, commence REAL NOT NULL, market_id TEXT NOT NULL, token_id TEXT NOT NULL, label TEXT NOT NULL,
        question TEXT NOT NULL, size REAL NOT NULL, cost REAL NOT NULL, fair REAL NOT NULL, edge REAL NOT NULL,
        order_id TEXT, payout REAL);
        INSERT INTO fills VALUES (1, 0, 'paper', 'nba', 'e', 0, 'm', 't', 'x', 'q', 10, 5, 0.5, 0.03, NULL, NULL);""")
    con.commit()
    con.close()
    ledger = Ledger(str(db))
    assert ledger.summary(venue="polymarket")["trades"] == 1
    assert ledger.summary(venue="polymarket-us")["trades"] == 0


def counting_gateway(handler):
    calls = []

    def wrapped(request):
        calls.append(request.url.path)
        return handler(request)
    sleeps = []
    venue = PolymarketUS(httpx.Client(transport=httpx.MockTransport(wrapped)), base_url="https://gw.test",
                         sleep=sleeps.append)
    return venue, calls, sleeps


def test_retries_after_429_respecting_retry_after():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"status": 429})
        return httpx.Response(200, json={"events": []})

    venue, calls, sleeps = counting_gateway(handler)
    assert venue.events("basketball_nba") == []
    assert len(calls) == 3 and sleeps.count(2.0) == 2


def test_requests_are_spaced_out():
    venue, calls, sleeps = counting_gateway(lambda r: httpx.Response(200, json=book_json(0.4, 0.5)))
    now = [0.0]
    venue.clock = lambda: now[0]
    venue.sleep = lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s))
    venue.book("m1:yes")
    venue.book("m2:yes")  # order books: 3 s apart
    venue.payout_per_share("m1:yes")  # other lookups aren't held up by the book spacing
    venue.payout_per_share("m2:yes")  # ...and are 0.1 s apart
    assert sleeps == [pytest.approx(3.0), pytest.approx(0.1)]


def test_both_teams_share_one_book_request():
    venue, calls, _ = counting_gateway(lambda r: httpx.Response(200, json=book_json(0.46, 0.48)))
    assert venue.book("aec-x:yes")["asks"][0]["price"] == 0.48
    assert venue.book("aec-x:no")["asks"][0]["price"] == pytest.approx(0.54)
    assert len(calls) == 1


def test_no_book_requests_when_listing_quotes_show_no_edge(tmp_path):
    # Fair prices equal to the listing's mids: nothing can clear a 3c edge, so no book is fetched.
    def handler(request):
        if request.url.path.startswith("/v2/leagues/nba/events"):
            events = load("pmus_nba_events.json")["events"] if request.url.params["offset"] == "0" else []
            return httpx.Response(200, json={"events": events})
        return httpx.Response(200, json=book_json(0.48, 0.49))

    venue, calls, _ = counting_gateway(handler)
    now = datetime(2026, 11, 4, 20, tzinfo=UTC)
    l = line("Detroit Pistons", "Philadelphia 76ers", NBA_START, {"Detroit Pistons": 0.515, "Philadelphia 76ers": 0.485}, updated=now)
    s = strat(tmp_path)
    bot = Bot(make_settings(), s, Odds([l]), venue, Trader(make_settings()), Ledger(s.db_path), clock=now.timestamp)
    decisions = bot.cycle(execute=False)
    assert len(decisions) == 2 and all(d.reason == "edge below threshold" for d in decisions)
    assert not any(p.endswith("/book") for p in calls)
