"""Run the CLI commands against a fake network built from the fixtures."""

from datetime import date
from pathlib import Path

import httpx
import pytest

from conftest import fixture_json, fixture_text
from wxbrackets import cli
from wxbrackets.db import DB
from wxbrackets.http import Http


ENSEMBLE_REQUESTS = []


def single_model(model: str) -> dict:
    """What Open-Meteo returns for one model: keys without a model suffix (as seen live)."""
    assert "," not in model, "models must be requested one at a time"
    ENSEMBLE_REQUESTS.append(model)
    payload = fixture_json("openmeteo_combined.json")
    hourly = {"time": payload["hourly"]["time"]}
    for key, values in payload["hourly"].items():
        if key.endswith("_" + model):
            hourly[key[: -len(model) - 1]] = values
    return {**payload, "hourly": hourly}


def fake_network(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "api.open-meteo.com/v1/forecast" in url:
        assert request.url.params["models"] == "ncep_hrrr_conus"
        return httpx.Response(200, json=fixture_json("openmeteo_hrrr.json"))
    if "ensemble-api.open-meteo.com" in url:
        assert request.url.params["timeformat"] == "unixtime"
        assert request.url.params["temperature_unit"] == "fahrenheit"
        return httpx.Response(200, json=single_model(request.url.params["models"]))
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
    ENSEMBLE_REQUESTS.clear()
    run("forecast", "--station", "CHI", "--date", "2026-11-03", "--brackets", "51,53,55,57")
    assert ENSEMBLE_REQUESTS == ["ecmwf_ifs025", "gfs025"]  # one request per model
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
    by_model = {}
    for line in out.splitlines():  # first table only; the lead-time table repeats the model names
        if line.split() and line.split()[0] in ("ncep_hrrr_conus", "gfs025", "ecmwf_ifs025"):
            by_model.setdefault(line.split()[0], line.split()[1:])
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


def test_forecast_uses_polymarket_us_brackets_and_calibration_compares(run, cfg, monkeypatch, capsys):
    from test_polymarket_us import EVENT

    def network(request):
        if request.url.host == "gateway.polymarket.us":
            if request.url.path.startswith("/v1/events/slug/temp-mdwhigh-"):
                return httpx.Response(200, json=fixture_json(EVENT))
            return httpx.Response(404)
        return fake_network(request)

    monkeypatch.setattr(cli, "make_http", lambda cfg, needs_nws=True: Http(
        "tests", client=httpx.Client(transport=httpx.MockTransport(network)), sleep=lambda s: None))
    run("forecast", "--station", "CHI", "--date", "2026-11-03")  # no --brackets: the market's are used
    out = capsys.readouterr().out
    assert "Polymarket US (edges are per contract, after the taker fee):" in out
    labels = [l.split()[0] for l in out.splitlines() if l.strip()[:1] in "≤≥6789" and "%" in l]
    assert labels == ["≤63", "64-65", "66-67", "68-69", "70-71", "≥72"]
    db = DB(cfg.database)
    assert db.conn.execute("SELECT COUNT(*) FROM market_prices").fetchone()[0] == 6

    run("backfill-outcomes", "--station", "CHI", "--days", "3")  # Nov 3 official high: 62 -> "≤63"
    run("calibration", "--station", "CHI")
    out = capsys.readouterr().out
    assert "Model vs. Polymarket US on the 1 runs with market prices" in out


def test_calibration_splits_model_errors_by_lead_time(cfg, tmp_path):
    from wxbrackets.calibration import calibrate, lead_bucket

    assert [lead_bucket(h) for h in (-3, 0, 10, 23.9, 24, 60)] == [
        "same day", "next day", "next day", "next day", "2+ days", "2+ days"]
    db = DB(cfg.database)
    base = dict(station="CHI", target_date="2026-11-03", bias_f=0, spread_inflation=1, bandwidth_f=1,
                dist_mean_f=60, observed_max_f=None, nws_forecast_high_f=None)
    for lead, rrfs_max, gfs_max in ((-4, 61.0, 64.0), (12, 59.0, 63.0), (40, 56.0, 62.0)):
        db.save_run({"created_utc": f"2026-11-0{3 if lead < 0 else 2}T00:00:00", "lead_hours": lead,
                     "pooled_mean_f": 60.0, **base},
                    [("rrfs", 0, rrfs_max, 0.5), ("gfs025", 1, gfs_max, 0.5)], [("≤60", None, 60, 0.5), ("≥61", 61, None, 0.5)])
    db.save_outcome("CHI", date(2026, 11, 3), 60, True, "test")
    res = calibrate(db, "CHI")
    cells = {(r.model, r.lead): (r.runs, round(r.bias_f, 2)) for r in res.per_model_lead.itertuples()}
    assert cells[("rrfs", "same day")] == (1, 1.0)
    assert cells[("rrfs", "next day")] == (1, -1.0)
    assert cells[("rrfs", "2+ days")] == (1, -4.0)
    assert cells[("gfs025", "2+ days")] == (1, 2.0)
