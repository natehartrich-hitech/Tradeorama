from datetime import date

import pytest

from conftest import fixture_text
from wxbrackets.cli_report import CliParseError, best_report, parse_cli, split_products


def test_final_report():
    r = parse_cli(fixture_text("cli_mdw_final.txt"))
    assert (r.location, r.climate_date, r.max_f, r.max_time, r.final) == ("MDW", date(2026, 11, 2), 58, "238 PM", True)


def test_same_day_report_with_record_flag():
    r = parse_cli(fixture_text("cli_mdw_sameday.txt"))
    assert (r.climate_date, r.max_f, r.final) == (date(2026, 11, 3), 61, False)


def test_missing_value():
    text = fixture_text("cli_mdw_final.txt").replace("MAXIMUM         58    238 PM", "MAXIMUM         MM      MM  ")
    r = parse_cli(text)
    assert r.max_f is None and r.max_time is None


def test_not_a_cli_report():
    with pytest.raises(CliParseError):
        parse_cli("NOUS43 KLOT 030000\nPNSLOT\nPUBLIC INFORMATION STATEMENT\n")


def test_archive_split_and_best_report():
    reports = [parse_cli(p) for p in split_products(fixture_text("cli_archive.txt"))]
    assert len(reports) == 3
    # Nov 3 has a same-day 61 and a final 62 issued the next morning: the final wins.
    assert best_report(reports, date(2026, 11, 3)).max_f == 62
    assert best_report(reports, date(2026, 11, 2)).max_f == 58
    assert best_report(reports, date(2026, 11, 4)) is None
    assert best_report(reports[:1], date(2026, 11, 3)).final is False  # partial if that's all there is
