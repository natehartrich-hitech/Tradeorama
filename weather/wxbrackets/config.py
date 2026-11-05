"""Settings loaded from a YAML file."""

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


class ConfigError(ValueError):
    pass


SOURCES = ("ensemble", "forecast", "rrfs", "refs")


@dataclass(frozen=True)
class Station:
    code: str
    name: str
    icao: str
    lat: float
    lon: float
    timezone: str
    cli_office: str
    cli_location: str
    pmus_event_prefix: str | None = None  # Polymarket US event slug prefix, e.g. "temp-mdwhigh"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    weight: float = 1.0
    expected_members: int | None = None
    source: str = "ensemble"  # ensemble / forecast (Open-Meteo APIs), rrfs / refs (NOAA AWS bucket)
    members: tuple[int, ...] | None = None  # refs only: which ensemble members (default 1-5)


@dataclass(frozen=True)
class DistributionSettings:
    bias_f: float = 0.0
    spread_inflation: float = 1.0
    bandwidth_f: float | None = None  # None = automatic
    min_valid_hours: int = 8
    edge_tolerance_hours: float = 3.0  # a member's data must reach this close to both ends of the climate day
    obs_tolerance_f: float = 0.5


@dataclass(frozen=True)
class PaperSettings:
    bankroll_usd: float = 200.0
    kelly_multiplier: float = 0.25
    min_edge: float = 0.03  # expected profit per contract after fees, in dollars
    max_event_usd: float = 25.0  # per city per day
    max_daily_usd: float = 100.0  # across all cities, per UTC day
    stop_hours_before_end: float = 2.0  # no new trades this close to the end of the climate day
    same_day_last_hour: float = 12.0  # no trades on TODAY's market from this hour, local standard time


@dataclass(frozen=True)
class Config:
    stations: dict[str, Station]
    models: list[Model]
    distribution: DistributionSettings
    default_brackets: list[int]
    database: Path
    nws_user_agent: str
    nws_min_interval: float = 1.0
    open_meteo_url: str = "https://ensemble-api.open-meteo.com/v1/ensemble"
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_key: str | None = None
    open_meteo_min_interval: float = 0.5
    rrfs_base_url: str = "https://noaa-rrfs-ops-pds.s3.amazonaws.com"
    pmus_gateway: str = "https://gateway.polymarket.us"
    paper: PaperSettings = PaperSettings()

    def station(self, code: str) -> Station:
        try:
            return self.stations[code.upper()]
        except KeyError:
            raise ConfigError(f"unknown station {code!r}; configured: {', '.join(sorted(self.stations))}") from None


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy config.example.yaml to {path.name} and edit it.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}  # Windows would default to cp1252
    return parse_config(raw, base_dir=path.parent)


def parse_config(raw: dict, base_dir: Path = Path(".")) -> Config:
    stations = {}
    for code, s in (raw.get("stations") or {}).items():
        try:
            st = Station(
                code=code.upper(),
                name=s["name"],
                icao=s["icao"].upper(),
                lat=float(s["lat"]),
                lon=float(s["lon"]),
                timezone=s["timezone"],
                cli_office=s["cli_office"].upper(),
                cli_location=s["cli_location"].upper(),
                pmus_event_prefix=s.get("pmus_event_prefix"),
            )
            st.tz  # validate the time zone name now rather than mid-run
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"station {code}: {exc}") from exc
        stations[st.code] = st
    if not stations:
        raise ConfigError("no stations configured")

    models = [
        Model(
            id=m["id"],
            name=m.get("name", m["id"]),
            weight=float(m.get("weight", 1.0)),
            expected_members=m.get("expected_members"),
            source=m.get("source", "ensemble"),
            members=tuple(int(x) for x in m["members"]) if m.get("members") else None,
        )
        for m in raw.get("models") or []
    ]
    bad = [m.id for m in models if m.source not in SOURCES]
    if bad:
        raise ConfigError(f"model source must be one of {', '.join(SOURCES)}: {', '.join(bad)}")
    if not models:
        raise ConfigError("no models configured")
    if any(m.weight < 0 for m in models) or sum(m.weight for m in models) <= 0:
        raise ConfigError("model weights must be non-negative and not all zero")

    d = raw.get("distribution") or {}
    bw = d.get("bandwidth_f", "auto")
    dist = DistributionSettings(
        bias_f=float(d.get("bias_f", 0.0)),
        spread_inflation=float(d.get("spread_inflation", 1.0)),
        bandwidth_f=None if bw in (None, "auto") else float(bw),
        min_valid_hours=int(d.get("min_valid_hours", 8)),
        edge_tolerance_hours=float(d.get("edge_tolerance_hours", 3.0)),
        obs_tolerance_f=float(d.get("obs_tolerance_f", 0.5)),
    )
    if dist.spread_inflation <= 0:
        raise ConfigError("spread_inflation must be positive")

    nws = raw.get("nws") or {}
    om = raw.get("open_meteo") or {}
    db = Path(raw.get("database", "wxbrackets.db"))
    return Config(
        stations=stations,
        models=models,
        distribution=dist,
        default_brackets=[int(x) for x in raw.get("default_brackets") or []],
        database=db if db.is_absolute() else base_dir / db,
        nws_user_agent=nws.get("user_agent", ""),
        nws_min_interval=float(nws.get("min_interval_sec", 1.0)),
        open_meteo_url=om.get("base_url") or "https://ensemble-api.open-meteo.com/v1/ensemble",
        open_meteo_forecast_url=om.get("forecast_url") or "https://api.open-meteo.com/v1/forecast",
        open_meteo_key=om.get("api_key") or None,
        open_meteo_min_interval=float(om.get("min_interval_sec", 0.5)),
        rrfs_base_url=(raw.get("rrfs") or {}).get("base_url") or "https://noaa-rrfs-ops-pds.s3.amazonaws.com",
        pmus_gateway=(raw.get("polymarket_us") or {}).get("gateway") or "https://gateway.polymarket.us",
        paper=PaperSettings(**{k: float(v) for k, v in (raw.get("paper") or {}).items()}),
    )
