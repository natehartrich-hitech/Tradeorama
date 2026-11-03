"""Run the CLI commands against a fake network built from the fixtures."""

from datetime import date
from pathlib import Path

import httpx
import pytest

from conftest import fixture_json, fixture_text
from wxbrackets import cli
from wxbrackets.db import DB
from wxbrackets.http import Http


def fake_network(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "api.open-meteo.com/v1/forecast" in url:
        assert request.url.params["models"] == "ncep_hrrr_conus"
        return httpx.Response(200, json=fixture_json("openmeteo_hrrr.json"))
    if "ensemble-api.open-meteo.com" in url:
        assert request.url.params["timeformat"] == "unixtime"
        assert request.url.params["temperature_unit"] == "fahrenheit"
        return httpx.Response(200, json=fixture_json("openmeteo_combined.json"))
    if "/points/" in url:
        return httpx.Response(200, json={"properties": {"forecast": "https://api.weather.gov/gridpoints/LOT/70,70/forecast"}})
    if "/gridpoints/" in url:
        return httpx.Response(200, json=fixture_json("nws_forecast.json"))
    if "/observations" in url:
        return httpx.Response(200, json=fixture_json("nws_observations.json"))
    if "mesonet.agron.iastate.edu" in url:
        return httpx.Response(200, text=fixture_text("cli_archive.txt"))
    if "/products/types/CLI/" in url:
        return httpx.Response(200, json={"@graph": []})
    return httpx.Response(404)


HRRR = """
  - id: ncep_hrrr_conus
    name: HRRR
    source: forecast
    weight: 0.5
    expected_members: 1
"""


@pytest.fixture
def run(cfg, tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(cli, "make_http", lambda cfg, needs_nws=True: Http(
        "tests", client=httpx.Client(transport=httpx.MockTransport(fake_network)), sleep=lambda s: None))
    monkeypatch.setattr(cli, "today_for", lambda station: date(2026, 11, 4))
    # Only the two models the fixture contains.
    text = config_path.read_text()
    text = text.split("models:")[0] + """models:
  - id: ecmwf_ifs025
    name: ECMWF
    expected_members: 3
  - id: gfs025
    name: GFS
    expected_members: 3
distribution:""" + text.split("distribution:")[1]
    config_path.write_text(text)

    def _run(*args, hrrr=False):
        if hrrr and "ncep_hrrr_conus" not in config_path.read_text():
            config_path.write_text(config_path.read_text().replace("distribution:", HRRR.lstrip("\n") + "distribution:", 1))
        cli.main(["--config", str(config_path), *args])
    return _run


def test_forecast_backfill_calibration(run, cfg, capsys):
    run("forecast", "--station", "CHI", "--date", "2026-11-03", "--brackets", "51,53,55,57")
    out = capsys.readouterr().out
    assert "NWS point forecast:     60°F" in out
    assert "    total  100.0%" in out
    assert "Saved as run 1" in out

    db = DB(cfg.database)
    members = db.conn.execute("SELECT model, member, member_max_f, weight FROM forecast_members ORDER BY model, member").fetchall()
    # gfs member 2 had no data; each model's weight is split across its own members.
    assert [(m["model"], m["member"], m["member_max_f"]) for m in members] == [
        ("ecmwf_ifs025", 0, 55.0), ("ecmwf_ifs025", 1, 56.0), ("ecmwf_ifs025", 2, 57.0),
        ("gfs025", 0, 52.0), ("gfs025", 1, 54.0),
    ]
    assert members[0]["weight"] == pytest.approx(1 / 6) and members[3]["weight"] == pytest.approx(1 / 4)
    assert db.conn.execute("SELECT pooled_mean_f FROM forecast_runs").fetchone()[0] == pytest.approx(54.5)

    run("backfill-outcomes", "--station", "CHI", "--days", "3")
    out = capsys.readouterr().out
    assert "2026-11-02  high 58°F" in out and "2026-11-03  high 62°F" in out

    run("calibration", "--station", "CHI")
    out = capsys.readouterr().out
    assert "1 forecast runs over 1 days" in out
    assert "Ensemble mean bias (raw mean - official): -7.50°F" in out


def test_same_day_forecast_conditions_on_observations(run, monkeypatch, capsys):
    monkeypatch.setattr(cli, "today_for", lambda station: date(2026, 11, 3))
    run("forecast", "--station", "CHI", "--date", "2026-11-03", "--brackets", "51,53,55,57,59", "--no-save")
    out = capsys.readouterr().out
    assert "Observed max so far:    60.1°F" in out
    # Observed 60.1 (tolerance 0.5 -> floor 59.6): everything at or below 59 is impossible.
    rows = {line.split()[0]: line.split()[1] for line in out.splitlines() if "%" in line and "¢" not in line}
    assert rows["≤51"] == "0.0%" and rows["58-59"] == "0.0%" and rows["≥60"] == "100.0%"


def test_observe(run, monkeypatch, capsys):
    monkeypatch.setattr(cli, "today_for", lambda station: date(2026, 11, 3))
    run("observe", "--station", "CHI")
    out = capsys.readouterr().out
    assert "Running max: 60.1°F" in out and "reports as 60°F" in out


def test_placeholder_user_agent_is_rejected(tmp_path):
    text = (Path(__file__).parent.parent / "config.example.yaml").read_text()
    path = tmp_path / "c.yaml"
    path.write_text(text)
    with pytest.raises(SystemExit, match="user_agent"):
        cli.main(["--config", str(path), "observe", "--station", "CHI"])


def test_hrrr_is_pooled_as_one_weighted_member(run, cfg, capsys):
    run("forecast", "--station", "CHI", "--date", "2026-11-03", "--brackets", "51,53,55,57", hrrr=True)
    out = capsys.readouterr().out
    [hrrr_line] = [line for line in out.splitlines() if line.strip().startswith("HRRR")]
    assert hrrr_line.split()[1:4] == ["1", "of", "1"] and "mean max 58.0°F  weight 0.5" in hrrr_line
    db = DB(cfg.database)
    w = dict(db.conn.execute("SELECT model, weight FROM forecast_members WHERE member = 0").fetchall())
    # Weights 1, 1, 0.5 -> ECMWF 0.4 over 3 members, GFS 0.4 over 2, HRRR 0.2 alone.
    assert w["ncep_hrrr_conus"] == pytest.approx(0.2)
    assert w["ecmwf_ifs025"] == pytest.approx(0.4 / 3)
    mean = db.conn.execute("SELECT pooled_mean_f FROM forecast_runs").fetchone()[0]
    assert mean == pytest.approx(0.4 * 56 + 0.4 * 53 + 0.2 * 58)

    run("backfill-outcomes", "--station", "CHI", "--days", "3")
    run("calibration", "--station", "CHI")
    out = capsys.readouterr().out
    # Official high was 62: each model's own mean vs. that.
    by_model = {line.split()[0]: line.split()[1:] for line in out.splitlines()
                if line.split() and line.split()[0] in ("ncep_hrrr_conus", "gfs025", "ecmwf_ifs025")}
    assert by_model["ncep_hrrr_conus"][:2] == ["1", "-4.00°"]
    assert by_model["gfs025"][:2] == ["1", "-9.00°"]
    assert by_model["ecmwf_ifs025"][:2] == ["1", "-6.00°"]


def test_forecast_includes_rrfs(run, cfg, tmp_path, monkeypatch, capsys):
    from test_rrfs import FakeBucket

    bucket = FakeBucket(lambda f: 289.0)  # 60.5°F every hour

    def network(request):
        return bucket(request) if request.url.host == "bucket.test" else fake_network(request)

    monkeypatch.setattr(cli, "make_http", lambda cfg, needs_nws=True: Http(
        "tests", client=httpx.Client(transport=httpx.MockTransport(network)), sleep=lambda s: None))
    path = tmp_path / "config.yaml"
    text = path.read_text().replace("distribution:", """  - id: rrfs
    name: RRFS
    source: rrfs
    weight: 0.5
    expected_members: 1
distribution:""", 1)
    path.write_text(text + "\nrrfs:\n  base_url: https://bucket.test\n")
    # A day the 06Z Nov 3 run covers, forecast before the 12Z run exists.
    monkeypatch.setattr(cli, "today_for", lambda station: date(2026, 11, 3))
    import wxbrackets.rrfs as rrfs_mod
    real = rrfs_mod.datetime

    class FrozenDatetime(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 11, 3, 11, tzinfo=tz)

    monkeypatch.setattr(rrfs_mod, "datetime", FrozenDatetime)
    run("forecast", "--station", "CHI", "--date", "2026-11-04", "--brackets", "55,57,59,61")
    out = capsys.readouterr().out
    [line] = [l for l in out.splitlines() if l.strip().startswith("RRFS")]
    assert line.split()[1:4] == ["1", "of", "1"] and "mean max 60.5°F" in line
