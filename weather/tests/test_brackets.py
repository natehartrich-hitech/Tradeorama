import math

import numpy as np
import pytest

from wxbrackets.brackets import make_brackets, parse_edges, round_half_up
from wxbrackets.distribution import HighDistribution, member_weights


def test_bracket_labels_and_bounds():
    bs = make_brackets(parse_edges("45,47,49,51,53,55"))
    assert [b.label for b in bs] == ["≤45", "46-47", "48-49", "50-51", "52-53", "54-55", "≥56"]
    assert (bs[0].lower_f, bs[0].upper_f) == (-math.inf, 45.5)
    assert (bs[1].lower_f, bs[1].upper_f) == (45.5, 47.5)
    assert (bs[-1].lower_f, bs[-1].upper_f) == (55.5, math.inf)
    assert bs[1].contains(46) and bs[1].contains(47) and not bs[1].contains(48)
    assert make_brackets([50, 51])[1].label == "51"


def test_edges_must_increase():
    with pytest.raises(ValueError):
        parse_edges("45,45,47")


def test_rounding_half_up_like_nws():
    assert [round_half_up(x) for x in (46.4, 46.5, 47.5, -0.5)] == [46, 47, 48, 0]


def test_probabilities_sum_to_one():
    bs = make_brackets([45, 47, 49, 51, 53, 55])
    d = HighDistribution.from_members(np.array([48.0, 50.0, 51.0, 53.5]), np.ones(4), spread_inflation=1.2)
    probs = d.bracket_probs(bs)
    assert sum(probs) == pytest.approx(1.0)
    assert all(p >= 0 for p in probs)


def test_rounding_boundaries_put_mass_in_the_right_bracket():
    bs = make_brackets([45, 47, 49])
    # A true high of 47.4 reports as 47 -> "46-47"; 47.6 reports as 48 -> "48-49".
    low = HighDistribution(np.array([47.4]), np.array([1.0]), bandwidth=0.02).bracket_probs(bs)
    high = HighDistribution(np.array([47.6]), np.array([1.0]), bandwidth=0.02).bracket_probs(bs)
    assert low[1] == pytest.approx(1.0) and high[2] == pytest.approx(1.0)


def test_single_gaussian_matches_closed_form():
    from scipy.stats import norm

    d = HighDistribution(np.array([50.0]), np.array([1.0]), bandwidth=2.0)
    [p] = d.bracket_probs(make_brackets([49, 51])[1:2])
    assert p == pytest.approx(norm.cdf(1.5 / 2) - norm.cdf(-0.5 / 2))


def test_bias_and_spread_inflation():
    values, w = np.array([48.0, 52.0]), np.ones(2)
    d = HighDistribution.from_members(values, w, bias_f=1.0, spread_inflation=1.5, bandwidth_f=1.0)
    assert list(d.centers) == [48.0, 54.0]  # mean 50 + 1.5 * (-2, +2) + 1
    assert d.mean() == pytest.approx(51.0)


def test_weights_are_per_model_not_per_member():
    w = member_weights({"ecmwf": 51, "gfs": 31}, {"ecmwf": 1.0, "gfs": 1.0})
    assert 51 * w["ecmwf"] == pytest.approx(0.5) and 31 * w["gfs"] == pytest.approx(0.5)
    w2 = member_weights({"ecmwf": 51, "gfs": 31, "gem": 0}, {"ecmwf": 2.0, "gfs": 1.0, "gem": 1.0})
    assert 51 * w2["ecmwf"] == pytest.approx(2 / 3) and "gem" not in w2


def test_conditioning_on_observed_max():
    bs = make_brackets([45, 47, 49, 51])
    d = HighDistribution.from_members(np.array([44.0, 46.0, 48.0, 50.0]), np.ones(4), bandwidth_f=1.0)
    c = d.conditioned_on_floor(47.6)  # already reached 48 (rounded)
    probs = c.bracket_probs(bs)
    assert probs[0] == 0 and probs[1] == 0
    assert sum(probs) == pytest.approx(1.0)
    assert c.mean() > d.mean()


def test_conditioning_above_every_member_pins_the_high_to_the_floor():
    d = HighDistribution(np.array([40.0]), np.array([1.0]), bandwidth=0.5).conditioned_on_floor(60.2)
    probs = d.bracket_probs(make_brackets([59, 61]))
    assert probs == [0.0, 1.0, 0.0]
