"""Per-city calibration: each station can override the shared settings and model weights, per kind."""

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from wxbrackets import cli
from wxbrackets.brackets import make_brackets
from wxbrackets.config import ConfigError, parse_config
from wxbrackets.forecast import build_forecast

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def config(**edits):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["distribution"]["bias_f"] = 0.5
    raw["distribution_low"] = {"spread_inflation": 1.6}
    for code, section in edits.items():
        raw["stations"][code].update(section)
    return parse_config(raw)


def test_settings_resolve_in_layers():
    cfg = config(CHI={"distribution_low": {"bias_f": -1.5}}, NYC={"distribution": {"bias_f": 1.2, "spread_inflation": 1.1}})
    chi, nyc, mia = cfg.station("CHI"), cfg.station("NYC"), cfg.station("MIA")
    # Chicago highs: shared. Chicago lows: shared lows (spread 1.6) + its own bias.
    assert (cfg.distribution_for("high", chi).bias_f, cfg.distribution_for("high", chi).spread_inflation) == (0.5, 1.3)
    assert (cfg.distribution_for("low", chi).bias_f, cfg.distribution_for("low", chi).spread_inflation) == (-1.5, 1.6)
    # New York highs: its own bias and spread; its lows: shared.
    assert (cfg.distribution_for("high", nyc).bias_f, cfg.distribution_for("high", nyc).spread_inflation) == (1.2, 1.1)
    assert cfg.distribution_for("low", nyc).bias_f == 0.5
    # A city without overrides, and no city at all: shared.
    assert cfg.distribution_for("high", mia) == cfg.distribution_for("high") == cfg.distribution


def test_city_model_weights():
    cfg = config(SFO={"weights": {"gem_global": 0, "ncep_hrrr_conus": 1.0}})
    sfo = cfg.weights_for("high", cfg.station("SFO"))
    assert sfo["gem_global"] == 0 and sfo["ncep_hrrr_conus"] == 1.0 and sfo["ecmwf_ifs025"] == 1.0
    assert cfg.weights_for("low", cfg.station("SFO"))["gem_global"] == 1.0  # lows untouched
    assert cfg.weights_for("high", cfg.station("CHI"))["gem_global"] == 1.0


@pytest.mark.parametrize("edits,match", [
    ({"CHI": {"distribution": {"bias": 1.0}}}, "unknown setting"),
    ({"CHI": {"distribution_low": {"spread_inflation": 0}}}, "spread_inflation must be positive"),
    ({"CHI": {"weights": {"no_such_model": 1.0}}}, "unknown model"),
])
def test_bad_city_settings_are_rejected(edits, match):
    with pytest.raises(ConfigError, match=match):
        config(**edits)


def test_city_settings_change_that_citys_forecast_only():
    cfg = config(CHI={"distribution": {"bias_f": -2.0}})
    maxes = {("ecmwf_ifs025", i): 60.0 for i in range(4)}
    brackets = make_brackets([58, 60, 62])
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    chi = build_forecast(cfg, cfg.station("CHI"), date(2026, 11, 4), maxes, brackets, now)
    nyc = build_forecast(cfg, cfg.station("NYC"), date(2026, 11, 4), maxes, brackets, now)
    assert chi.dist.mean() == pytest.approx(58.0) and nyc.dist.mean() == pytest.approx(60.5)


def test_city_weight_of_zero_drops_the_model():
    cfg = config(SFO={"weights": {"gfs025": 0}})
    maxes = {("ecmwf_ifs025", 0): 60.0, ("gfs025", 0): 70.0}
    f = build_forecast(cfg, cfg.station("SFO"), date(2026, 11, 4), maxes, make_brackets([60, 65]),
                       datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert [m for m, *_ in f.members] == ["ecmwf_ifs025"]


def test_calibration_prints_a_ready_to_paste_city_correction(capsys):
    cfg = config()
    res = SimpleNamespace(mean_bias_f=1.23, days=12)  # the raw forecasts ran 1.23°F too warm
    cli.print_bias_suggestion(cfg, cfg.station("CHI"), "low", res)
    out = capsys.readouterr().out
    assert "Correction in use for CHI lows: bias_f +0.5 (from the shared settings)" in out
    assert "      distribution_low:\n        bias_f: -1.2" in out
    assert "Only 12 days so far" in out
    cfg2 = config(CHI={"distribution_low": {"bias_f": -1.2}})
    cli.print_bias_suggestion(cfg2, cfg2.station("CHI"), "low", SimpleNamespace(mean_bias_f=1.2, days=40))
    out = capsys.readouterr().out
    assert "from this city's own distribution_low" in out and "Only" not in out
