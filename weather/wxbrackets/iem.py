"""Archived CLI products from the Iowa Environmental Mesonet (IEM) AFOS text archive.

api.weather.gov only keeps recent products, so backfills older than about a week
come from here. The IEM is a free academic service: keep requests small.
"""

from datetime import date, timedelta

from .cli_report import CliParseError, CliReport, parse_cli, split_products
from .config import Station
from .http import Http

AFOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"


class IEM:
    def __init__(self, http: Http):
        self.http = http

    def cli_reports(self, station: Station, first: date, last: date) -> list[CliReport]:
        """CLI reports issued from `first` through the morning after `last`, oldest first."""
        params = {
            "pil": f"CLI{station.cli_location}",
            "fmt": "text",
            "sdate": first.isoformat(),
            "edate": (last + timedelta(days=2)).isoformat(),
            "limit": 9999,
            "order": "asc",
        }
        text = self.http.get(AFOS_URL, params=params).text
        reports = []
        for product in split_products(text):
            try:
                reports.append(parse_cli(product))
            except CliParseError:
                continue
        return reports
