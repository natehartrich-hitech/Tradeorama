"""api.weather.gov: point forecast high, station observations, and CLI products."""

from dataclasses import dataclass
from datetime import date, datetime, timezone

from .cli_report import CliParseError, CliReport, parse_cli
from .climate_day import climate_day_window
from .config import Station
from .http import Http

NWS_API = "https://api.weather.gov"
GEOJSON = {"Accept": "application/geo+json"}
LDJSON = {"Accept": "application/ld+json"}


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


@dataclass(frozen=True)
class RunningMax:
    max_f: float | None
    at: datetime | None
    observations: int
    latest: datetime | None


def parse_point_forecast_high(forecast: dict, day: date, station: Station) -> int | None:
    """Daytime period temperature for `day` from a /gridpoints/.../forecast response."""
    for period in (forecast.get("properties") or {}).get("periods") or []:
        if not period.get("isDaytime"):
            continue
        start = datetime.fromisoformat(period["startTime"]).astimezone(station.tz)
        if start.date() == day and period.get("temperatureUnit", "F") == "F":
            return period.get("temperature")
    return None


def parse_running_max(observations: dict, start: datetime, end: datetime) -> RunningMax:
    best = best_at = latest = None
    count = 0
    for feat in observations.get("features") or []:
        props = feat.get("properties") or {}
        temp = props.get("temperature") or {}
        value = temp.get("value")
        if value is None or temp.get("qualityControl") == "X":  # X = rejected by NWS quality control
            continue
        ts = datetime.fromisoformat(props["timestamp"]).astimezone(timezone.utc)
        if not (start <= ts < end):
            continue
        unit = temp.get("unitCode", "wmoUnit:degC")
        f = c_to_f(value) if unit.endswith("degC") else float(value)
        count += 1
        latest = ts if latest is None or ts > latest else latest
        if best is None or f > best:
            best, best_at = f, ts
    return RunningMax(best, best_at, count, latest)


class NWS:
    def __init__(self, http: Http):
        self.http = http

    def forecast_high(self, station: Station, day: date) -> int | None:
        point = self.http.json(f"{NWS_API}/points/{station.lat:.4f},{station.lon:.4f}", headers=GEOJSON)
        url = point["properties"]["forecast"]
        return parse_point_forecast_high(self.http.json(url, headers=GEOJSON), day, station)

    def running_max(self, station: Station, day: date, now: datetime | None = None) -> RunningMax:
        start, end = climate_day_window(day, station.tz)
        now = now or datetime.now(timezone.utc)
        params = {"start": start.isoformat(), "end": min(end, now).isoformat()}
        obs = self.http.json(f"{NWS_API}/stations/{station.icao}/observations", params=params, headers=GEOJSON)
        return parse_running_max(obs, start, end)

    def recent_cli_reports(self, station: Station, since: date, limit: int = 60) -> list[CliReport]:
        """CLI products issued on or after `since` (UTC), oldest first. The API keeps only recent days."""
        listing = self.http.json(f"{NWS_API}/products/types/CLI/locations/{station.cli_location}", headers=LDJSON)
        items = [
            p for p in listing.get("@graph") or []
            if p.get("issuingOffice", "").endswith(station.cli_office) and p.get("issuanceTime", "") >= since.isoformat()
        ]
        items = sorted(items, key=lambda p: p.get("issuanceTime", ""))[-limit:]
        reports = []
        for item in items:
            product = self.http.json(f"{NWS_API}/products/{item['id']}", headers=LDJSON)
            try:
                reports.append(parse_cli(product.get("productText") or ""))
            except CliParseError:
                continue
        return reports
