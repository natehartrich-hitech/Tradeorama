"""Kalshi structural scan: pre-set rules for bets, fees, fills, choices and kill rules, plus fetch -> report."""

from argparse import Namespace
from datetime import datetime, timezone

import httpx
import pytest

from tradeorama.replay.kalshi import Kalshi
from tradeorama.scan import analysis as an
from tradeorama.scan.__main__ import cmd_fetch, cmd_report, hash_frac, main, pick_series, reference


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


REF = ts("2026-06-10T20:00:00")


def candle(end, bid, ask, low=None, high=None, volume=1000.0):
    return an.Candle(int(end), bid, ask, low, high, volume)


def market(candles, result="yes", ref=REF, open_ts=None, close_ts=None, group="Financials", maker=0.0, weight=1.0,
           event="E1"):
    return an.Market("T", event, group, ref, open_ts if open_ts is not None else ref - 48 * 3600,
                     close_ts if close_ts is not None else ref, result, 0.07, maker, weight, tuple(candles))


def test_fee_rounds_each_order_up_to_the_cent():
    assert an.fee(0.07, 0.92) == pytest.approx(0.0052)  # 0.07 × 100 × 0.92 × 0.08 = 51.52¢ -> 52¢
    assert an.fee(0.0, 0.92) == 0
    assert an.fee_coefs("quadratic", 1) == (0.07, 0.0)
    assert an.fee_coefs("quadratic_with_maker_fees", 0.5) == (0.035, 0.00875)


def test_yes_favorite_taker_and_maker():
    t = REF - 3600
    m = market([candle(t, 0.90, 0.92, volume=500), candle(t + 3600, 0.95, 0.96, low=0.91, high=0.97)])
    bets = an.bets_for(m, 1)
    taker, maker = [b for b in bets if b.style == "taker"], [b for b in bets if b.style == "maker"]
    assert [(b.side, b.price) for b in taker] == [("yes", 0.92)]
    assert taker[0].cost == pytest.approx(0.92 + 0.0052) and taker[0].won
    assert taker[0].hour_dollars == pytest.approx(500 * 0.92)
    assert [(b.side, b.price) for b in maker] == [("yes", 0.91)]  # improves the 0.90 bid; a trade at 0.91 fills it
    assert an.bets_for(m, 6) == [] and an.bets_for(m, 24) == []  # no quote then


def test_no_side_and_queue_rules():
    t = REF - 6 * 3600
    # YES 0.06 / 0.08: NO's ask is 0.94. A NO bid at 0.93 means selling YES at 0.07.
    m = market([candle(t - 600, 0.06, 0.08), candle(t + 1800, 0.05, 0.07, low=0.05, high=0.07)], result="no")
    bets = an.bets_for(m, 6)
    assert [(b.style, b.side, b.price, b.won) for b in bets] == [("taker", "no", 0.94, True), ("maker", "no", 0.93, True)]
    # A 1¢ spread: the maker bid joins the queue at 0.91, so only a trade below 0.91 fills it.
    joined = market([candle(t, 0.91, 0.92), candle(t + 3600, 0.91, 0.92, low=0.91, high=0.92)])
    assert [b.style for b in an.bets_for(joined, 6)] == ["taker"]
    through = market([candle(t, 0.91, 0.92), candle(t + 3600, 0.89, 0.92, low=0.90, high=0.92)])
    assert [b.style for b in an.bets_for(through, 6)] == ["taker", "maker"]


def test_no_bet_without_an_open_market_or_a_fresh_two_sided_quote():
    t = REF - 3600
    assert an.bets_for(market([candle(t, 0.90, 0.92)], close_ts=t - 1), 1) == []  # closed early: already settled
    assert an.bets_for(market([candle(t - 3600, 0.90, 0.92)]), 1) == []  # quote over an hour old
    assert an.bets_for(market([candle(t, 0.0, 0.92)]), 1) == []  # one-sided book
    assert an.bets_for(market([candle(t, 0.50, 0.52)]), 1) == []  # not a favorite
    assert an.bets_for(market([candle(t, 0.97, 0.98)]), 1) == []  # above 97¢
    assert an.bets_for(market([candle(t, 0.90, 0.92)], result="scalar"), 1) == []


def bet(day, won, price=0.90, group="Crypto", hours=1, style="taker", weight=1.0, event=None, dollars=100.0):
    m = market([], ref=ts(day + "T12:00:00") + 3600 * hours, group=group, weight=weight, event=event or day + group)
    return an.Bet(m, hours, style, "yes", price, price, won, dollars)


def test_summary_months_capacity_and_luck():
    bets = [bet(f"2026-0{m}-{d:02d}", won=(d % 10 != 0)) for m in (5, 6, 7, 8) for d in range(1, 29)]
    s = an.summarize(bets, an.HOLDOUT, an.HOLDOUT_MONTHS)
    wins = sum(b.won for b in bets)
    assert s.bets == 112 and s.days == 112
    assert s.ret == pytest.approx((wins * 100 / 0.9 - 112 * 100) / (112 * 100 / 0.9))
    assert set(s.months) == set(an.HOLDOUT_MONTHS)
    assert s.weekly_dollars == pytest.approx(112 * 100 / (123 / 7))
    assert 0 <= s.luck_pct <= 100
    losers = [bet(f"2026-05-{d:02d}", won=False) for d in range(1, 20)]
    assert an.summarize(losers, an.HOLDOUT).luck_pct > 50


def test_choices_come_from_the_fit_period_only():
    fit = ([bet("2026-01-05", won=True, group="Crypto", hours=6) for _ in range(250)]
           + [bet("2026-01-05", won=False, group="Crypto", hours=1) for _ in range(250)]
           + [bet("2026-01-05", won=True, group="Economics", hours=24) for _ in range(100)])  # too few to choose
    a, b, c = an.choose(fit)
    assert a.picks == ((None, 6),)
    assert b.picks == (("Crypto", 6),)
    assert c.picks == () and "no group" in c.note
    hold = [bet("2026-06-05", True, group="Crypto", hours=6), bet("2026-06-05", True, group="Crypto", hours=1),
            bet("2026-06-05", True, group="Economics", hours=6, style="maker")]
    assert len(a.select(hold)) == 1 and len(b.select(hold)) == 1


def test_kill_rules():
    good = an.Summary(1000, 500, 100, 0.015, 0.9, 0.92, 1.0,
                      {"2026-05": 0.01, "2026-06": 0.02, "2026-07": -0.01, "2026-08": 0.02}, 5000.0)
    assert [r.passed for r in an.kill_rules(good)] == [True, True, True, True]
    thin = an.Summary(1000, 500, 100, 0.015, 0.9, 0.92, 8.0,
                      {"2026-05": 0.01, "2026-06": -0.02, "2026-07": -0.01, "2026-08": 0.02}, 500.0)
    assert [r.passed for r in an.kill_rules(thin)] == [True, False, False, False]


def test_calibration_buckets():
    t = REF - 3600
    ms = [market([candle(t, 0.90, 0.92)], result="yes"), market([candle(t, 0.90, 0.92)], result="no"),
          market([candle(t, 0.02, 0.04)], result="no")]
    rows = an.calibration(ms, an.HOLDOUT, 1)
    assert [(r[0], r[1]) for r in rows] == [("0–5¢", 1), ("90–95¢", 2)]
    assert rows[1][3] == 0.5


def test_reference_time_is_fixed_in_advance():
    m = {"close_time": "2026-04-16T04:59:00Z", "expected_expiration_time": "2026-04-16T14:00:00Z"}
    assert reference(m, sports=False) == (ts("2026-04-16T04:59:00"), "close")  # no early-close condition
    weather = {**m, "early_close_condition": "The Last Trading Time will be 11:59 PM ET on April 15, 2026 regardless "
                                             "of any data releases or events occurring."}
    assert reference(weather, sports=False)[1] == "close"
    nominee = {"close_time": "2026-03-05T14:22:00Z", "expected_expiration_time": "2029-01-20T15:00:00Z",
               "early_close_condition": "This market will close and expire early following the next person nominated."}
    assert reference(nominee, sports=False) == (ts("2029-01-20T15:00:00"), "expected")
    game = {**m, "early_close_condition": "This market will close and expire after a winner is declared."}
    assert reference(game, sports=True) == (ts("2026-04-16T14:00:00"), "expected")


def test_sampling_is_deterministic_and_picks_top_series():
    assert hash_frac("KXHIGHNY-26JUN10") == hash_frac("KXHIGHNY-26JUN10")
    assert 0 <= hash_frac("x") < 1
    rows = [{"ticker": f"S{i}", "category": "Crypto", "volume": 2e6 + i, "status": None} for i in range(5)]
    rows += [{"ticker": "BIG", "category": "Sports", "volume": 9e9, "status": None},
             {"ticker": "SMALL", "category": "Crypto", "volume": 10, "status": None},
             {"ticker": "COMBO", "category": "Exotics", "volume": 9e9, "status": None}]
    assert [r["ticker"] for r in pick_series(rows, 2, 1)] == ["S4", "S3", "BIG"]


# ---------------------------------------------------------------- fetch -> report


def kalshi_transport():
    """One financial series with daily events in June 2026, two markets each, and a series of 15-minute markets."""
    days = [f"2026-06-{d:02d}" for d in range(1, 11)]

    def handler(request):
        path, q = request.url.path, request.url.params
        if path.endswith("/historical/cutoff"):
            return httpx.Response(200, json={"market_settled_ts": "2026-09-13T00:00:00Z"})
        if path.endswith("/series"):
            return httpx.Response(200, json={"series": [
                {"ticker": "KXINX", "category": "Financials", "fee_type": "quadratic", "fee_multiplier": 1, "volume_fp": "5000000"},
                {"ticker": "KXBTC15M", "category": "Crypto", "fee_type": "quadratic", "fee_multiplier": 1, "volume_fp": "9000000"},
            ]})
        if path.endswith("/events"):
            if q["series_ticker"] == "KXBTC15M":
                return httpx.Response(200, json={"events": [{"event_ticker": "KXBTC15M-26JUN011200"}], "cursor": ""})
            return httpx.Response(200, json={"cursor": "", "events": [{"event_ticker": f"KXINX-{d}"} for d in days]})
        if path.endswith("/historical/markets"):
            if "series_ticker" in q:  # the whole archive of a small series: built from the per-event answers
                evs = ["KXINX-" + d for d in days] if q["series_ticker"] == "KXINX" else ["KXBTC15M-26JUN011200"]
                ms = [m for ev in evs for m in handler(httpx.Request("GET", f"https://x/historical/markets?event_ticker={ev}")).json()["markets"]]
                for m in ms:
                    m["event_ticker"] = m["ticker"].rsplit("-", 1)[0]
                return httpx.Response(200, json={"markets": ms, "cursor": ""})
            ev = q["event_ticker"]
            if ev.startswith("KXBTC15M"):
                return httpx.Response(200, json={"markets": [{"ticker": ev + "-A", "result": "yes", "open_time": "2026-06-01T11:45:00Z",
                                                              "close_time": "2026-06-01T12:00:00Z"}]})
            d = ev.split("-", 1)[1]
            return httpx.Response(200, json={"markets": [
                {"ticker": f"{ev}-HI", "result": "yes", "open_time": f"{d}T00:00:00Z", "close_time": f"{d}T20:00:00Z",
                 "settlement_ts": f"{d}T20:05:00Z"},
                {"ticker": f"{ev}-LO", "result": "no", "open_time": f"{d}T00:00:00Z", "close_time": f"{d}T20:00:00Z",
                 "settlement_ts": f"{d}T20:05:00Z"}]})
        if "/candlesticks" in path:
            ticker = path.split("/")[-2]
            end = int(q["end_ts"])
            hi = ticker.endswith("-HI")
            out = []
            for t in range(int(q["start_ts"]) // 3600 * 3600 + 3600, end + 1, 3600):
                bid, ask = ("0.9000", "0.9200") if hi else ("0.0400", "0.0600")
                out.append({"end_period_ts": t, "yes_bid": {"close": bid}, "yes_ask": {"close": ask},
                            "price": {"low": bid, "high": ask}, "volume": "2000.00"})
            return httpx.Response(200, json={"candlesticks": out})
        raise AssertionError(f"unexpected request {path}")
    return httpx.MockTransport(handler)


def test_fetch_then_report(tmp_path):
    db = str(tmp_path / "scan.db")
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport()), min_interval=0, sleep=lambda s: None)
    lines = []
    args = Namespace(db=db, per_category=25, sports=30, per_month=50, markets_per_event=3, interval=0)
    cmd_fetch(args, kalshi, out=lines.append)
    text = "\n".join(lines)
    assert "Series: 2 chosen" in text and "Candles: 20 markets" in text
    out = []
    cmd_report(Namespace(db=db), out=out.append)
    report = "\n".join(out)
    assert "Series left out: 1 (markets open less than an hour before their time)" in report
    assert "Reference times: 20 markets from a fixed close, 0 from the scheduled expiration" in report
    # 20 markets, each a favorite on one side at 6 h and 1 h (they open at midnight, so not 24 h), taker and maker
    assert "Bets at 85–97¢: 0 in fit (Oct–Apr), 80 in holdout (May–Aug)" in report
    assert "not enough fit-period bets" in report and "Verdict: every hypothesis fails" in report
    # A rerun downloads nothing new.
    lines.clear()
    cmd_fetch(args, kalshi, out=lines.append)
    assert "0 still to sample" in "\n".join(lines) and "Candles: 0 markets" in "\n".join(lines)


def test_report_saves_a_utf8_file(tmp_path, capsys):
    db = str(tmp_path / "scan.db")
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport()), min_interval=0, sleep=lambda s: None)
    cmd_fetch(Namespace(db=db, per_category=25, sports=30, per_month=50, markets_per_event=3, interval=0), kalshi,
              out=lambda s: None)
    target = tmp_path / "scan_report.txt"
    main(["report", "--db", db, "--out", str(target)])
    text = target.read_text(encoding="utf-8")
    assert "contracts priced 85–97¢" in text and "Verdict:" in text
    assert f"Saved to {target}" in capsys.readouterr().out


# ---------------------------------------------------------------- leads


def test_spread_rule_skips_empty_books():
    t = REF - 3600
    empty = market([candle(t, 0.12, 0.88)])  # both asks are 88¢: the first run bought both sides
    assert [b.side for b in an.bets_for(empty, 1) if b.style == "taker"] == ["yes", "no"]
    assert an.bets_for(empty, 1, max_spread=0.03) == []
    tight = market([candle(t, 0.90, 0.92)])
    assert [b.side for b in an.bets_for(tight, 1, max_spread=0.03)] == ["yes"]


def test_lead1_takes_only_tight_economics_takers():
    t = REF - 6 * 3600
    econ = market([candle(t, 0.90, 0.92), candle(t + 3600, 0.90, 0.92, low=0.90, high=0.92)], group="Economics")
    other = market([candle(t, 0.90, 0.92)], group="Financials")
    wide = market([candle(t, 0.80, 0.92)], group="Economics")
    bets = an.lead1_bets([econ, other, wide])
    assert [(b.market.group, b.style, b.hours) for b in bets] == [("Economics", "taker", 6)]


def test_favorite_bets_buy_the_favorite_side_at_its_ask():
    t = REF - 3600
    yes_fav = market([candle(t, 0.74, 0.76)], result="yes", ref=REF)
    no_fav = market([candle(t, 0.19, 0.21)], result="yes", ref=REF)  # NO's mid is 80¢; NO's ask is 81¢; NO lost
    wide = market([candle(t, 0.60, 0.90)], result="yes", ref=REF)  # mid 75¢, but a 30¢ spread
    pairs = an.favorite_bets([yes_fav, no_fav, wide], an.HOLDOUT, 1, 0.65, 0.90)
    assert [(b.side, b.price, round(mid, 3), b.won) for b, mid in pairs] == [
        ("yes", 0.76, 0.75, True), ("no", 0.81, 0.80, False), ("yes", 0.90, 0.75, True)]
    tight = an.favorite_bets([yes_fav, no_fav, wide], an.HOLDOUT, 1, 0.65, 0.90, max_spread=0.03)
    assert len(tight) == 2
    row = an.favorite_row(tight)
    assert row.markets == 2 and row.won == 0.5 and row.ask == pytest.approx((0.76 + 0.81) / 2)
    assert an.favorite_bets([yes_fav], an.FIT, 1, 0.65, 0.90) == []  # June is not in the fit period


def test_lead2_needs_enough_markets_and_profit():
    row = lambda n, r: an.FavoriteRow(n, n, 0.75, 0.76, 0.8, r, 10.0)
    assert an.lead2_survives(row(60, 0.035))
    assert not an.lead2_survives(row(40, 0.10))
    assert not an.lead2_survives(row(60, 0.02))
    assert not an.lead2_survives(an.FavoriteRow(0, 0, None, None, None, None, None))


def test_leads_command_runs_on_the_cache(tmp_path):
    db = str(tmp_path / "scan.db")
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport()), min_interval=0, sleep=lambda s: None)
    cmd_fetch(Namespace(db=db, per_category=25, sports=30, per_month=50, markets_per_event=3, interval=0), kalshi,
              out=lambda s: None)
    target = tmp_path / "leads.txt"
    main(["leads", "--db", db, "--out", str(target)])
    text = target.read_text(encoding="utf-8")
    assert "Lead 1: Economics" in text and "Lead 1: fails a kill rule" in text  # the mock has no Economics markets
    assert "1 h before, spread ≤ 3¢ (the deciding row)" in text and "Lead 2: doesn't survive" in text
