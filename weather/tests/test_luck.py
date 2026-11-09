"""Is it luck? Margins of error for paper P&L and for the model-vs-market Brier comparison."""

from types import SimpleNamespace

import pytest
from scipy.stats import norm

from wxbrackets import cli, paper
from wxbrackets.calibration import brier_difference
from wxbrackets.db import DB


def fills(db):
    common = dict(station="CHI", target_date="2026-11-04", kind="high")
    # Bracket A, bought twice (a top-up): 20 contracts for $2.00, model 15%, it WON.
    db.record_paper_fill(slug="a", side="yes", label="64-65", contracts=10, cost=1.0, prob=0.15, edge=0.04, **common)
    db.record_paper_fill(slug="a", side="yes", label="64-65", contracts=10, cost=1.0, prob=0.15, edge=0.04, **common)
    # Bracket B: 30 contracts for $3.00, model 14%, it lost.
    db.record_paper_fill(slug="b", side="yes", label="66-67", contracts=30, cost=3.0, prob=0.14, edge=0.03, **common)
    for f in db.unsettled_paper():
        db.settle_paper(f["id"], f["contracts"] if f["slug"] == "a" else 0.0)


def test_luck_check_numbers(tmp_path):
    db = DB(tmp_path / "l.db")
    fills(db)
    luck = paper.luck_check(db)
    assert luck.positions == 2  # the top-up counts once: they win or lose together
    assert luck.pnl == pytest.approx(15.0)
    assert luck.model_expected == pytest.approx(20 * 0.15 - 2 + 30 * 0.14 - 3)
    # No skill: each contract wins with the price paid (10c): var = 20^2*.1*.9 + 30^2*.1*.9 = 117.
    assert luck.null_sd == pytest.approx(117 ** 0.5)
    assert luck.chance_pct == pytest.approx(100 * norm.sf(15 / 117 ** 0.5))  # about 8%: can't rule out luck


def test_luck_check_ignores_open_positions(tmp_path):
    db = DB(tmp_path / "o.db")
    db.record_paper_fill(station="CHI", target_date="2026-11-04", kind="high", slug="a", side="yes", label="x",
                         contracts=10, cost=1.0, prob=0.2, edge=0.05)
    assert paper.luck_check(db) is None


def test_paper_report_prints_the_luck_check(tmp_path, capsys):
    cfg = SimpleNamespace(database=tmp_path / "r.db")
    fills(DB(cfg.database))
    cli.cmd_paper_report(cfg, SimpleNamespace(station=None, kind=None, limit=5))
    out = capsys.readouterr().out
    assert "Is it luck? 2 settled positions, P&L +$15.00" in out
    assert "happens about 8% of the time -> can't rule out luck yet" in out


def test_brier_difference_uses_days_not_runs():
    # Day 1 had two runs; they share one outcome, so they're averaged first.
    res = brier_difference({"d1": [-0.1, -0.2], "d2": [-0.05], "d3": [-0.15]})
    assert res["brier_days"] == 3
    assert res["brier_diff"] == pytest.approx((-0.15 - 0.05 - 0.15) / 3)
    assert res["brier_diff_se"] == pytest.approx(0.0577350 / 3 ** 0.5, rel=1e-4)


@pytest.mark.parametrize("diff,se,verdict", [
    (-0.12, 0.03, "model clearly better"),
    (0.10, 0.02, "market clearly better"),
    (-0.02, 0.03, "not clear yet"),
    (-0.02, None, "needs at least 2"),
])
def test_brier_margin_verdicts(capsys, diff, se, verdict):
    cli.print_brier_margin(SimpleNamespace(brier_diff=diff, brier_diff_se=se, brier_days=10))
    assert verdict in capsys.readouterr().out
