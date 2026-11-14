from tradeorama.heartbeat import Heartbeat

from test_strategy import book, make_bot

URL = "https://hc-ping.com/abc"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def recorder():
    sent = []
    return sent, lambda url, body: sent.append((url, body))


def test_no_url_is_a_no_op():
    sent, send = recorder()
    hb = Heartbeat(None, send=send)
    hb.beat(force=True)
    hb.fail("boom")
    assert sent == []


def test_beats_are_throttled_unless_forced():
    sent, send = recorder()
    clock = Clock()
    hb = Heartbeat(URL, every_sec=600, clock=clock, send=send)
    hb.beat()
    clock.t = 599
    hb.beat()
    hb.beat(force=True)
    assert sent == [(URL, None), (URL, None)]


def test_fail_posts_the_reason():
    sent, send = recorder()
    Heartbeat(URL, send=send).fail("boom")
    assert sent == [(URL + "/fail", b"boom")]


def test_network_errors_never_reach_the_bot(capsys):
    def broken(url, body):
        raise OSError("offline")

    hb = Heartbeat(URL, send=broken)
    hb.beat()
    hb.fail("x")
    assert capsys.readouterr().err.count("heartbeat ping failed") == 1


def test_run_loop_beats_after_each_cycle(tmp_path):
    sent, send = recorder()
    bot = make_bot(tmp_path, {"lakers": book(0.53, 0.54), "celtics": book(0.45, 0.46)})
    bot.heartbeat = Heartbeat(URL, send=send)
    bot.run(interval=300, max_cycles=1)
    assert sent == [(URL, None)]


def test_strategy_settings_read_heartbeat_url(monkeypatch):
    from tradeorama.config import load_strategy_settings

    monkeypatch.setenv("STRATEGY_HEARTBEAT_URL", f' "{URL}" ')
    assert load_strategy_settings().heartbeat_url == URL
    monkeypatch.setenv("STRATEGY_HEARTBEAT_URL", "")
    assert load_strategy_settings().heartbeat_url is None
