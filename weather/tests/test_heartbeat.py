import types

import pytest

from wxbrackets import cli
from wxbrackets.heartbeat import Heartbeat

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
    hb = Heartbeat("  ", send=send)
    hb.beat(force=True)
    hb.fail("boom")
    assert not hb.enabled and sent == []


def test_beats_are_throttled_unless_forced():
    sent, send = recorder()
    clock = Clock()
    hb = Heartbeat(URL + "/", every_sec=600, clock=clock, send=send)
    hb.beat()
    clock.t = 599
    hb.beat()
    assert sent == [(URL, None)]  # trailing slash dropped, second beat throttled
    hb.beat(force=True)
    clock.t = 1199
    hb.beat()
    clock.t = 1200
    hb.beat()
    assert len(sent) == 3


def test_fail_posts_the_reason():
    sent, send = recorder()
    Heartbeat(URL, send=send).fail("ValueError('bad')")
    assert sent == [(URL + "/fail", b"ValueError('bad')")]


def test_network_errors_never_reach_the_bot(capsys):
    def broken(url, body):
        raise OSError("no route to host")

    clock = Clock()
    hb = Heartbeat(URL, clock=clock, send=broken)
    hb.beat()
    clock.t = 1000
    hb.beat()
    hb.fail("x")
    assert capsys.readouterr().err.count("heartbeat ping failed") == 1  # reported once, not every time


def test_sleep_keeps_beating(monkeypatch):
    sent, send = recorder()
    clock = Clock()
    monkeypatch.setattr("time.sleep", lambda s: setattr(clock, "t", clock.t + s))
    hb = Heartbeat(URL, every_sec=600, clock=clock, send=send)
    hb.beat(force=True)
    hb.sleep(3600)
    assert clock.t == 3600
    assert len(sent) == 1 + 6  # one ping per 10 minutes of waiting


def test_paper_reports_a_crash_and_still_raises(monkeypatch):
    sent, send = recorder()
    real_init = Heartbeat.__init__
    monkeypatch.setattr(Heartbeat, "__init__", lambda self, url: real_init(self, url, send=send))

    def crash(cfg, args, hb):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli, "paper_loop", crash)
    cfg = types.SimpleNamespace(heartbeat_url=URL)
    with pytest.raises(RuntimeError):
        cli.cmd_paper(cfg, types.SimpleNamespace(dry=False))
    assert sent == [(URL + "/fail", b"wxbrackets paper stopped: RuntimeError('database is locked')")]
    sent.clear()
    with pytest.raises(RuntimeError):
        cli.cmd_paper(cfg, types.SimpleNamespace(dry=True))  # dry runs never ping
    assert sent == []


def test_config_reads_heartbeat_url(cfg):
    assert cfg.heartbeat_url is None  # the example config leaves it empty
