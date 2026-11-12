"""Replay of Kalshi sports trades against Pinnacle. Kalshi fixtures are real responses (November 2026, game of
Feb 28 2026); Odds API historical responses are built in the documented shape."""

import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from tradeorama.replay import analysis as an
from tradeorama.replay.__main__ import cmd_fetch, cmd_report, start_looks_wrong
from tradeorama.replay.store import Store
from tradeorama.replay.history import OddsHistory, estimated_credits, snapshot_times
from tradeorama.replay.kalshi import Kalshi, parse_time, parse_trade
from tradeorama.replay.teams import event_date, match_event

FIXTURES = Path(__file__).parent / "fixtures"
MARKETS = json.loads((FIXTURES / "kalshi_nba_markets.json").read_text(encoding="utf-8"))
TRADES = json.loads((FIXTURES / "kalshi_nba_trades.json").read_text(encoding="utf-8"))
TIP = parse_time("2026-03-01T02:00:00Z")  # New Orleans at Utah, 9 PM Eastern on Feb 28


def utc(s: str) -> float:
    return parse_time(s)


# ---------------------------------------------------------------- Kalshi


def test_parse_real_trades():
    trades = [parse_trade(t) for t in TRADES["trades"]]
    assert all(trades)
    first = trades[0]
    assert first.ticker == "KXNBAGAME-26FEB28NOPUTA-NOP"
    assert first.yes_price == pytest.approx(0.69) and first.contracts == 35 and first.taker_yes and not first.block
    sides = {t.taker_yes for t in trades}
    assert sides == {True, False}  # the fixture has takers on both sides


def test_parse_trade_falls_back_to_legacy_fields():
    t = parse_trade({"trade_id": "x", "ticker": "T", "created_time": "2026-02-01T00:00:00Z",
                     "yes_price": 45, "count": 3, "taker_side": "no"})
    assert t.yes_price == pytest.approx(0.45) and t.contracts == 3 and not t.taker_yes
    assert parse_trade({"trade_id": "x", "ticker": "T", "created_time": "2026-02-01T00:00:00Z"}) is None


def kalshi_with(handler) -> Kalshi:
    return Kalshi(httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0, sleep=lambda s: None)


def test_trades_pages_and_splits_archive_from_live():
    cutoff = utc("2026-03-01T01:58:00Z")
    archived = [t for t in TRADES["trades"] if utc(t["created_time"]) < cutoff]
    live = [t for t in TRADES["trades"] if utc(t["created_time"]) >= cutoff]
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/historical/trades"):
            if request.url.params.get("cursor") == "p2":
                return httpx.Response(200, json={"trades": archived[5:], "cursor": ""})
            return httpx.Response(200, json={"trades": archived[:5], "cursor": "p2"})
        assert request.url.path.endswith("/markets/trades")
        return httpx.Response(200, json={"trades": live + archived[:1], "cursor": ""})  # overlap is deduped

    out = kalshi_with(handler).trades("KXNBAGAME-26FEB28NOPUTA-NOP", TIP - 6 * 3600, TIP, cutoff)
    assert len(out) == len(TRADES["trades"])
    assert [t.ts for t in out] == sorted(t.ts for t in out)
    assert [p for p, _ in seen].count("/trade-api/v2/historical/trades") == 2
    assert seen[0][1]["limit"] == "1000" and seen[0][1]["min_ts"] == str(int(TIP - 6 * 3600))


def test_settled_markets_reads_archive_and_live_tier():
    def handler(request):
        if request.url.path.endswith("/historical/markets"):
            return httpx.Response(200, json=MARKETS)
        assert request.url.params["status"] == "settled"
        return httpx.Response(200, json={"markets": [MARKETS["markets"][0]], "cursor": ""})

    markets = kalshi_with(handler).settled_markets("KXNBAGAME", 0, 2e9, settled_cutoff=1e9)
    assert sorted(m["ticker"] for m in markets) == ["KXNBAGAME-26FEB28NOPUTA-NOP", "KXNBAGAME-26FEB28NOPUTA-UTA"]


def test_kalshi_waits_out_rate_limits():
    calls, slept = [], []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": "too many requests"})
        return httpx.Response(200, json={"market_settled_ts": "2026-09-13T00:00:00Z"})

    k = Kalshi(httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0, sleep=slept.append)
    assert k.cutoff() == {"market_settled_ts": utc("2026-09-13T00:00:00Z")}
    assert [s for s in slept if s >= 1] == [1, 2]  # waits after each 429, besides the pacing between requests
    assert k.min_interval > 0  # and it now paces itself more slowly


def test_kalshi_honours_retry_after():
    calls, slept = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        return httpx.Response(200, json={})

    Kalshi(httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0, sleep=slept.append).cutoff()
    assert 3.0 in slept


# ---------------------------------------------------------------- matching and planning


def odds_game(gid, home, away, commence):
    return {"id": gid, "sport_key": "basketball_nba", "home_team": home, "away_team": away,
            "commence_time": commence, "commence_ts": utc(commence)}


def test_event_date():
    assert str(event_date("KXNBAGAME-26FEB28NOPUTA")) == "2026-02-28"
    assert event_date("KXNBAGAME-XYZ") is None


def test_match_event_by_nickname_and_date():
    games = [
        odds_game("a", "Utah Jazz", "New Orleans Pelicans", "2026-03-01T02:00:00Z"),
        odds_game("b", "Utah Jazz", "New Orleans Pelicans", "2026-03-03T02:00:00Z"),  # a rematch, wrong date
        odds_game("c", "Los Angeles Lakers", "LA Clippers", "2026-03-01T03:30:00Z"),
    ]
    game, mapping = match_event("basketball_nba", "KXNBAGAME-26FEB28NOPUTA", ["UTA", "NOP"], games)
    assert game["id"] == "a" and mapping == {"UTA": "Utah Jazz", "NOP": "New Orleans Pelicans"}
    game, mapping = match_event("basketball_nba", "KXNBAGAME-26FEB28LACLAL", ["LAC", "LAL"], games)
    assert game["id"] == "c" and mapping == {"LAC": "LA Clippers", "LAL": "Los Angeles Lakers"}
    assert match_event("basketball_nba", "KXNBAGAME-26FEB28GUANOP", ["GUA", "NOP"], games) == (
        None, "unknown team code GUA")
    assert match_event("basketball_nba", "KXNBAGAME-26FEB20NOPUTA", ["UTA", "NOP"], games)[0] is None


def test_snapshot_times_cover_the_window_and_closing_lines():
    starts = [utc("2026-03-01T00:00:00Z"), utc("2026-03-01T02:10:00Z")]
    times = snapshot_times(starts, hours=6)
    assert times[0] == utc("2026-02-28T18:00:00Z")
    assert utc("2026-03-01T02:00:00Z") in times and utc("2026-03-01T02:09:00Z") in times  # off-grid start
    assert max(times) <= max(starts)
    assert len(times) == 17 + 1  # 18:00 to 02:00 every 30 minutes, plus 02:09
    assert estimated_credits(22, 6) == 22 * (1 + 10 * 21)


def test_odds_history_tracks_credits_and_hides_the_key():
    def handler(request):
        if "events" in request.url.path:
            return httpx.Response(401, text=f"bad key {request.url.params['apiKey']}")
        assert request.url.params["bookmakers"] == "pinnacle" and request.url.params["date"] == "2026-03-01T01:30:00Z"
        return httpx.Response(200, json={"timestamp": "2026-03-01T01:25:39Z", "data": []},
                              headers={"x-requests-remaining": "9000", "x-requests-last": "10"})

    odds = OddsHistory("SECRETKEY", httpx.Client(transport=httpx.MockTransport(handler)))
    assert odds.odds("basketball_nba", utc("2026-03-01T01:30:00Z"))["data"] == []
    assert odds.remaining == 9000 and odds.spent == 10
    with pytest.raises(RuntimeError) as exc:
        odds.events("basketball_nba", 0)
    assert "SECRETKEY" not in str(exc.value) and "paid plan" in str(exc.value)


# ---------------------------------------------------------------- analysis

T0 = utc("2026-02-28T23:00:00Z")
PRM = an.Params()


def game(fairs, winner="Utah Jazz", commence=TIP):
    """Snapshots every 30 minutes from T0 with Utah's no-vig probability `fairs[i]`."""
    snaps = tuple(an.Snapshot(T0 + 1800 * i, {"Utah Jazz": f, "New Orleans Pelicans": 1 - f}) for i, f in enumerate(fairs))
    return an.Game("KXNBAGAME-26FEB28NOPUTA", "2026-02-28", commence, "Utah Jazz", "New Orleans Pelicans", snaps, winner)


def test_replay_counts_only_stable_gaps_that_clear_fees():
    g = game([0.50, 0.50, 0.52, 0.53])  # Pinnacle moves after the second snapshot; closes at 0.53
    trades = [
        an.Trade(T0 + 60, "Utah Jazz", 0.45, 100),  # 0.50 − 0.45 − 0.0173 fee = +3.3¢, line steady: a gap
        an.Trade(T0 + 120, "Utah Jazz", 0.44, 50),  # same window
        an.Trade(T0 + 1800 + 60, "Utah Jazz", 0.40, 100),  # Pinnacle moved 0.50 -> 0.52 after this: not counted
        an.Trade(T0 + 200, "Utah Jazz", 0.48, 100),  # edge too small after fees
        an.Trade(T0 + 300, "Utah Jazz", 0.20, 100),  # 30¢ off Pinnacle: bad data, not money
        an.Trade(T0 + 5400 + 60, "Utah Jazz", 0.30, 100),  # after the closing snapshot
        an.Trade(T0 - 7200, "Utah Jazz", 0.30, 100),  # before the first snapshot
        an.Trade(TIP + 60, "Utah Jazz", 0.30, 100),  # in-game: ignored
    ]
    rep = an.replay([g], {g.key: trades}, PRM)
    assert rep.games == 1 and rep.days == 1 and rep.trades == 7
    assert rep.skipped["moved"] == 1 and rep.skipped["small"] == 1 and rep.skipped["divergent"] == 1
    assert rep.skipped["after_snapshots"] == 1 and rep.skipped["before_snapshots"] == 1
    [w] = rep.windows
    assert w.trades == 2 and w.contracts == 150 and w.dollars == pytest.approx(45 + 22)
    unit = lambda p: p + 0.07 * p * (1 - p)
    assert w.avg_cost == pytest.approx((100 * unit(0.45) + 50 * unit(0.44)) / 150)
    assert w.close == 0.53 and w.clv == pytest.approx(0.53 - w.avg_cost)
    # $100 stake capped by the $67 (plus fees) that traded at qualifying prices; Utah won.
    assert w.spend(100) == pytest.approx(w.cost_total)
    assert w.pnl(100) == pytest.approx(150 - w.cost_total)


def test_fee_waiver_and_illinois_tax():
    g = game([0.50, 0.50])
    t = an.Trade(T0 + 60, "Utah Jazz", 0.47, 10, fee_waived=True)
    assert an.replay([g], {g.key: [t]}, PRM).windows[0].avg_cost == pytest.approx(0.47)
    taxed = an.replay([g], {g.key: [t]}, an.Params(il_tax=0.0175)).windows
    assert taxed == []  # 0.50 − 0.47 × 1.0175 = 2.2¢ < 3¢


def test_kill_rules():
    g = game([0.50, 0.50, 0.50])
    rep = an.replay([g], {g.key: [an.Trade(T0 + 60, "Utah Jazz", 0.40, 1000)]}, PRM)
    rules = an.kill_rules(rep)
    assert [r.passed for r in rules] == [True, True, True]
    thin = an.replay([g], {g.key: [an.Trade(T0 + 60, "Utah Jazz", 0.40, 10)]}, PRM)
    assert [r.passed for r in an.kill_rules(thin)] == [True, False, True]
    none = an.replay([g], {g.key: [an.Trade(T0 + 60, "Utah Jazz", 0.49, 10)]}, PRM)
    assert [r.passed for r in an.kill_rules(none)] == [False, False, False]


def test_luck_check_and_clv_range():
    windows = []
    for day in range(10):
        g = an.Game(f"G{day}", f"2026-02-{day + 1:02d}", T0 + 4 * 3600 + day * 86400, "H", "A",
                    tuple(an.Snapshot(T0 + day * 86400 + 1800 * i, {"H": 0.5, "A": 0.5}) for i in range(3)),
                    winner="H" if day % 2 else "A")
        rep = an.replay([g], {g.key: [an.Trade(T0 + day * 86400 + 60, "H", 0.40, 100)]}, PRM)
        windows += rep.windows
    luck = an.luck_check(windows, 100)
    assert luck.games == 10 and luck.pnl == pytest.approx(5 * 100 - sum(w.cost_total for w in windows))
    assert luck.close_expected > 0 and 0 < luck.chance_pct < 50
    lo, hi = an.clv_range(windows, 100)
    assert lo == pytest.approx(hi)  # every window has the same CLV


# ---------------------------------------------------------------- end to end


def odds_transport(credits: list):
    def handler(request):
        credits.append(1)
        headers = {"x-requests-remaining": str(10000 - 10 * len(credits)), "x-requests-last": "1"}
        if request.url.path.endswith("/events"):
            data = [{"id": "ev1", "sport_key": "basketball_nba", "commence_time": "2026-03-01T02:00:00Z",
                     "home_team": "Utah Jazz", "away_team": "New Orleans Pelicans"}]
            return httpx.Response(200, json={"timestamp": request.url.params["date"], "data": data}, headers=headers)
        at = utc(request.url.params["date"])
        snap = at - 120
        nop = 0.62 if at < utc("2026-03-01T01:30:00Z") else 0.66  # Pinnacle's New Orleans price, then the close
        prices = [{"name": "New Orleans Pelicans", "price": round(1.03 / nop, 3)},
                  {"name": "Utah Jazz", "price": round(1.03 / (1 - nop), 3)}]
        ev = {"id": "ev1", "sport_key": "basketball_nba", "commence_time": "2026-03-01T02:00:00Z",
              "home_team": "Utah Jazz", "away_team": "New Orleans Pelicans",
              "bookmakers": [{"key": "pinnacle", "last_update": request.url.params["date"],
                              "markets": [{"key": "h2h", "last_update": request.url.params["date"], "outcomes": prices}]}]}
        headers["x-requests-last"] = "10"
        stamp = datetime.fromtimestamp(snap, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return httpx.Response(200, json={"timestamp": stamp, "data": [ev]}, headers=headers)
    return httpx.MockTransport(handler)


def kalshi_transport():
    def handler(request):
        path = request.url.path
        if path.endswith("/historical/cutoff"):
            return httpx.Response(200, json={"market_settled_ts": "2026-09-13T00:00:00Z",
                                             "trades_created_ts": "2026-09-13T00:00:00Z"})
        if path.endswith("/series/KXNBAGAME"):
            return httpx.Response(200, json={"series": {"ticker": "KXNBAGAME", "fee_type": "quadratic_with_maker_fees",
                                                        "fee_multiplier": 1}})
        if path.endswith("/historical/markets"):
            return httpx.Response(200, json=MARKETS)
        if path.endswith("/historical/trades"):
            ticker = request.url.params["ticker"]
            return httpx.Response(200, json={"trades": [t for t in TRADES["trades"] if t["ticker"] == ticker], "cursor": ""})
        raise AssertionError(f"unexpected Kalshi request {path}")
    return httpx.MockTransport(handler)


def fetch_args(db, **kw):
    base = dict(sport="basketball_nba", series=None, start="2026-02-28", end="2026-02-28", hours=6, step=30,
                db=str(db), max_credits=6000, keep_credits=2500, yes=False)
    return Namespace(**{**base, **kw})


def report_args(db, **kw):
    base = dict(sport="basketball_nba", series=None, start="2026-02-28", end="2026-02-28", hours=6, step=30,
                db=str(db), min_edge=0.03, stake=100, il_tax=0.0, fee_coef=None, tolerance=0.005,
                max_divergence=0.15, top=5)
    return Namespace(**{**base, **kw})


def test_fetch_asks_before_spending(tmp_path):
    credits, lines = [], []
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport()), min_interval=0, sleep=lambda s: None)
    make_odds = lambda: OddsHistory("k", httpx.Client(transport=odds_transport(credits)))
    cmd_fetch(fetch_args(tmp_path / "r.db"), kalshi, make_odds, ask=lambda q: "n", out=lines.append)
    assert credits == [] and "Nothing was spent." in lines


def test_fetch_then_report(tmp_path):
    db = tmp_path / "r.db"
    credits, lines = [], []
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport()), min_interval=0, sleep=lambda s: None)
    make_odds = lambda: OddsHistory("k", httpx.Client(transport=odds_transport(credits)))
    cmd_fetch(fetch_args(db), kalshi, make_odds, ask=lambda q: "y", out=lines.append)
    text = "\n".join(lines)
    assert "Kalshi: 1 KXNBAGAME game on 1 day" in text
    assert "Matched 1 of 1 Kalshi games" in text
    assert "Pinnacle snapshots: 13 to fetch, 130 credits." in text  # 20:00 to 02:00 UTC every 30 minutes
    assert len(credits) == 1 + 13
    # A second run pays for nothing.
    credits.clear()
    cmd_fetch(fetch_args(db, yes=True), kalshi, make_odds, out=lines.append)
    assert credits == []

    out = []
    cmd_report(report_args(db), out=out.append)
    report = "\n".join(out)
    assert "Games: 1 on Kalshi, 1 matched to Pinnacle, 1 with snapshots and pregame trades, on 1 game day" in report
    assert "Kill rules (set before looking)" in report and "Verdict:" in report
    # The fixture's trades are 01:13–02:00 UTC; New Orleans traded at 0.67–0.69 against Pinnacle's 0.62 then 0.66:
    # no gaps, and the line moved at 01:30.
    assert "Windows: 0" in report


def test_start_looks_wrong():
    start = utc("2026-03-01T02:30:00Z")
    assert start_looks_wrong("basketball_nba", start, [utc("2026-03-01T03:43:00Z"), None])  # 1 h 13 min: impossible
    assert not start_looks_wrong("basketball_nba", start, [utc("2026-03-01T05:00:00Z")])
    assert not start_looks_wrong("basketball_nba", start, [None])


def test_report_leaves_out_games_with_a_wrong_start(tmp_path):
    db = tmp_path / "r.db"
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport()), min_interval=0, sleep=lambda s: None)
    make_odds = lambda: OddsHistory("k", httpx.Client(transport=odds_transport([])))
    cmd_fetch(fetch_args(db, yes=True), kalshi, make_odds, out=lambda s: None)
    store = Store(str(db))
    store.db.execute("UPDATE games SET commence = ?", (utc("2026-03-01T04:30:00Z"),))  # Kalshi closed at 05:17
    store.commit()
    out = []
    cmd_report(report_args(db), out=out.append)
    report = "\n".join(out)
    assert "Left out 1 game whose Kalshi market closed too soon" in report
    assert "KXNBAGAME-26FEB28NOPUTA: listed start 03-01 04:30 UTC, Kalshi closed 03-01 05:17 UTC" in report
    assert "Nothing to replay" in report
