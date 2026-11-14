"""Market-making test: Kalshi's reward scoring, the fill simulation, the collector and the report."""

import httpx
import pytest

from tradeorama.mm import rewards as rw
from tradeorama.mm import sim
from tradeorama.mm.collect import Collector, eligible
from tradeorama.mm.report import report, score, summary
from tradeorama.mm.store import Store
from tradeorama.replay.kalshi import Kalshi


# ---------------------------------------------------------------- scoring


def test_side_share_follows_kalshis_rules():
    # Our 100 join the best bid at 45¢. Target 1,000: the reference price is where cumulative size reaches 200,
    # which is 44¢ (150 at 45¢, then 100 more). 40¢ is 4 ticks below it: 0.5 ** 4 = 1/16 credit.
    book = [(0.45, 50), (0.44, 100), (0.40, 1000)]
    ok, share = rw.side_share(book, target=1000, discount=0.5, ours=100)
    assert ok and share == pytest.approx(100 / (150 + 100 + 1000 / 16))
    assert rw.side_share(book, target=2000, discount=0.5, ours=100) == (False, 0.0)  # not deep enough
    assert rw.side_share([], target=100, discount=0.5, ours=100) == (False, 0.0)  # we don't quote empty sides
    ok, share = rw.side_share(book, target=1000, discount=0.5, ours=0)
    assert ok and share == 0.0


def test_snapshot_value_needs_both_sides():
    prog = rw.Program(start=0, end=100_000, reward=100.0, target=1000, discount=0.5)  # $0.001 a second
    yes = [(0.45, 50), (0.44, 100), (0.40, 1000)]
    no = [(0.53, 50), (0.52, 100), (0.48, 1000)]
    earned, counted = rw.snapshot_value(yes, no, [prog], ts=10, seconds=60, ours_yes=100, ours_no=100)
    share = 100 / (150 + 100 + 1000 / 16)
    assert counted and earned == pytest.approx(0.001 * 60 * (share + share) / 2)
    thin = [(0.53, 50)]
    assert rw.snapshot_value(yes, thin, [prog], 10, 60, 100, 100) == (0.0, False)
    assert rw.snapshot_value(yes, no, [prog], 200_000, 60, 100, 100) == (0.0, False)  # program over


# ---------------------------------------------------------------- simulation


def books(*items):
    return [sim.Book(ts, tuple(yes), tuple(no)) for ts, yes, no in items]


def test_fills_respect_the_queue_and_trades_through():
    bs = books((0, [(0.45, 50)], [(0.53, 80)]), (60, [(0.45, 50)], [(0.53, 80)]), (120, [(0.44, 10)], [(0.53, 80)]))
    trades = [
        sim.Trade(30, 0.45, 70, taker_yes=False),  # sold YES at 45¢: 50 ahead of us, then 20 fill our YES bid
        sim.Trade(40, 0.47, 50, taker_yes=True),  # bought YES at 47¢ = sold NO at 53¢: all 50 go to the 80 ahead
        sim.Trade(90, 0.43, 5, taker_yes=False),  # through our 45¢ bid: our whole remaining YES bid fills
    ]
    res = sim.simulate(bs, trades, [], size=100, cap=300)
    assert [(f.side, f.price, f.qty) for f in res.fills] == [("yes", 0.45, 20), ("yes", 0.45, 100)]
    assert res.position == {"yes": 120, "no": 0}
    assert res.cost["yes"] == pytest.approx(120 * 0.45)
    # The last book has YES at 44¢ bid and 47¢ ask (1 - 53¢): mid 45.5¢.
    assert sim.value(res, bs, None) == pytest.approx(120 * 0.455)
    assert sim.value(res, bs, "no") == 0.0 and sim.value(res, bs, "yes") == 120


def test_cap_stops_a_side_and_markouts():
    bs = books((0, [(0.30, 10)], [(0.68, 10)]), (60, [(0.30, 10)], [(0.68, 10)]), (3700, [(0.20, 10)], [(0.78, 10)]))
    trades = [sim.Trade(10, 0.29, 500, taker_yes=False)]  # sweeps through our 30¢ YES bid
    res = sim.simulate(bs, trades, [], size=100, cap=100)
    assert res.position["yes"] == 100
    # An hour later YES is 20¢ bid / 22¢ ask: mid 21¢, so each contract lost 9¢ to adverse selection.
    marks = sim.markouts(res, bs)
    assert len(marks) == 100 and marks[0] == pytest.approx(0.21 - 0.30)


def test_gaps_longer_than_five_minutes_are_not_credited():
    prog = rw.Program(0, 100_000, 100.0, 100, 0.5)
    bs = books((0, [(0.45, 200)], [(0.53, 200)]), (3600, [(0.45, 200)], [(0.53, 200)]))
    res = sim.simulate(bs, [], [prog], size=100, cap=300)
    assert res.seconds == sim.MAX_GAP_SEC


# ---------------------------------------------------------------- selection


def test_freight_and_sports_are_left_out():
    assert eligible({"ticker": "KXRT", "title": "Rotten Tomatoes score", "category": "Entertainment"})
    for s in ({"ticker": "KXSUEZWEEKLY", "title": "Suez Canal transits", "category": "Economics"},
              {"ticker": "KXPANAMAWEEKLY", "title": "Panama", "category": "Economics"},
              {"ticker": "KXPORTX", "title": "Port of LA container volume", "category": "Economics"},
              {"ticker": "KXTRUCK", "title": "Trucking tonnage", "category": "Economics"},
              {"ticker": "KXNBAGAME", "title": "NBA Game", "category": "Sports"}):
        assert not eligible(s), s
    assert not eligible(None)


# ---------------------------------------------------------------- collector and report


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def kalshi_transport(clock):
    def handler(request):
        path, q = request.url.path, request.url.params
        if path.endswith("/series"):
            return httpx.Response(200, json={"series": [
                {"ticker": "KXRT", "title": "Rotten Tomatoes", "category": "Entertainment", "fee_type": "quadratic", "fee_multiplier": 1},
                {"ticker": "KXSUEZWEEKLY", "title": "Suez Canal transits", "category": "Economics", "fee_type": "quadratic"},
            ]})
        if path.endswith("/incentive_programs"):
            progs = [{"id": f"p{i}", "market_ticker": t, "series_ticker": t.split("-")[0],
                      "start_date": "2026-11-14T00:00:00Z", "end_date": "2026-11-20T00:00:00Z",
                      "period_reward": 1_000_000, "target_size_fp": "100.00", "discount_factor_bps": 5000}
                     for i, t in enumerate(["KXRT-A-80", "KXRT-B-90", "KXSUEZWEEKLY-X-50"])]
            return httpx.Response(200, json={"incentive_programs": progs, "cursor": ""})
        if path.endswith("/markets/orderbooks"):
            tickers = q.get_list("tickers")
            return httpx.Response(200, json={"orderbooks": [
                {"ticker": t, "orderbook_fp": {"yes_dollars": [["0.4400", "100.00"], ["0.4500", "60.00"]],
                                               "no_dollars": [["0.5200", "100.00"], ["0.5300", "60.00"]]}}
                for t in tickers]})
        if path.endswith("/markets/trades"):
            ts = clock.t - 30
            return httpx.Response(200, json={"cursor": "", "trades": [
                {"trade_id": f"{q['ticker']}-{int(ts)}", "ticker": q["ticker"], "count_fp": "80.00",
                 "yes_price_dollars": "0.4500", "no_price_dollars": "0.5500", "taker_outcome_side": "no",
                 "created_time": __import__("datetime").datetime.fromtimestamp(ts, tz=__import__("datetime").timezone.utc).isoformat()}]})
        if path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [{"ticker": t, "status": "active", "result": ""}
                                                         for t in q["tickers"].split(",")]})
        raise AssertionError(f"unexpected {path}")
    return httpx.MockTransport(handler)


def test_collector_then_report(tmp_path):
    from datetime import datetime, timezone
    clock = FakeClock(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport(clock)), min_interval=0, sleep=lambda s: None)
    store = Store(str(tmp_path / "mm.db"))
    lines = []
    col = Collector(kalshi, store, days=20 / 1440, clock=clock, sleep=clock.sleep, out=lines.append,
                    stop_file=str(tmp_path / "mm.stop"))
    assert col.run() == "done"
    c = store.counts()
    assert c["markets"] == 2  # the Suez market is left out
    assert c["books"] == 2 * 20 and c["trades"] > 0
    # A restart doesn't extend the test: the end time is kept in mm.db.
    again = Collector(kalshi, store, days=5, clock=clock, sleep=clock.sleep, out=lines.append,
                      stop_file=str(tmp_path / "mm.stop"))
    assert again.ends == col.ends and again.run() == "done"

    by_size = score(store)
    s = summary(by_size[100])
    assert s["rewards"] > 0 and s["fills"] > 0 and s["capital"] > 0
    out = []
    report(store, out=out.append)
    text = "\n".join(out)
    assert "Kill rules (at 100 contracts a side)" in text and "Verdict:" in text
    assert "100 contracts *" in text


def test_stop_file_stops_the_collector(tmp_path):
    from datetime import datetime, timezone
    clock = FakeClock(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    kalshi = Kalshi(httpx.Client(transport=kalshi_transport(clock)), min_interval=0, sleep=lambda s: None)
    (tmp_path / "mm.stop").write_text("")
    col = Collector(kalshi, Store(str(tmp_path / "mm.db")), days=1, clock=clock, sleep=clock.sleep, out=lambda s: None,
                    stop_file=str(tmp_path / "mm.stop"))
    assert col.run() == "stopped"


def test_unchanged_books_are_stored_as_a_marker_and_read_back(tmp_path):
    from tradeorama.mm.report import load
    st = Store(str(tmp_path / "mm.db"))
    st.save_book("M", 1.0, [[0.45, 100.0]], [[0.53, 100.0]])
    st.save_book("M", 2.0, "=", "=")
    st.save_book("M", 3.0, [[0.46, 50.0]], [])
    bs, _, _ = load(st, "M")
    assert [b.yes for b in bs] == [((0.45, 100.0),), ((0.45, 100.0),), ((0.46, 50.0),)]
    assert bs[2].no == ()
