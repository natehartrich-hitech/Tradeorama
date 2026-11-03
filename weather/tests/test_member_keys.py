import numpy as np
import pytest

from conftest import fixture_json
from wxbrackets.openmeteo import parse_member_key, parse_members

MODELS = ["ecmwf_ifs025", "gfs025", "icon_seamless", "icon_global"]


@pytest.mark.parametrize("key,expected", [
    ("temperature_2m", (None, 0)),                                # single model, control
    ("temperature_2m_member07", (None, 7)),                       # single model, member
    ("temperature_2m_gfs025", ("gfs025", 0)),                     # multi-model control
    ("temperature_2m_member30_gfs025", ("gfs025", 30)),           # member then model
    ("temperature_2m_gfs025_member30", ("gfs025", 30)),           # model then member
    ("temperature_2m_member39_icon_seamless", ("icon_seamless", 39)),
    ("temperature_2m_member03_icon_global", ("icon_global", 3)),  # not confused with icon_seamless
    ("temperature_2m_max", None),                                 # different variable
    ("temperature_80m_member01", None),
    ("time", None),
])
def test_parse_member_key(key, expected):
    assert parse_member_key(key, "temperature_2m", MODELS) == expected


def test_parse_combined_response():
    members = parse_members(fixture_json("openmeteo_combined.json"), ["ecmwf_ifs025", "gfs025"])
    assert [(m.model, m.member) for m in members] == [
        ("ecmwf_ifs025", 0), ("ecmwf_ifs025", 1), ("ecmwf_ifs025", 2),
        ("gfs025", 0), ("gfs025", 1), ("gfs025", 2),
    ]
    assert np.isnan(members[-1].values).all()  # nulls become NaN


def test_key_without_model_is_ambiguous_for_multi_model_requests():
    payload = {"hourly": {"time": [0], "temperature_2m_member01": [50.0]}}
    with pytest.raises(ValueError, match="no model id"):
        parse_members(payload, ["ecmwf_ifs025", "gfs025"])
    [only] = parse_members(payload, ["gfs025"])
    assert (only.model, only.member) == ("gfs025", 1)


def test_forecast_api_keys_for_deterministic_models():
    # Forecast API: no member numbers; a model suffix only when several models are requested.
    assert parse_member_key("temperature_2m_ncep_hrrr_conus", "temperature_2m", ["ncep_hrrr_conus"]) == ("ncep_hrrr_conus", 0)
    [hrrr] = parse_members(fixture_json("openmeteo_hrrr.json"), ["ncep_hrrr_conus"])
    assert (hrrr.model, hrrr.member) == ("ncep_hrrr_conus", 0)


def test_unmatched_keys_are_reported_not_silently_dropped(caplog):
    # A combined request whose keys carry different model labels than were asked for.
    payload = {"hourly": {"time": [0], "temperature_2m_member01_ncep_gefs025": [50.0], "temperature_2m_ncep_gefs025": [49.0]}}
    with caplog.at_level("WARNING"):
        assert parse_members(payload, ["gfs025", "ecmwf_ifs025"]) == []
    assert "ignored 2 temperature_2m keys" in caplog.text
