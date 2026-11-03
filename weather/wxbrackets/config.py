"""Settings loaded from a YAML file."""

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


class ConfigError(ValueError):
    pass


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

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    weight: float = 1.0
    expected_members: int | None = None


@dataclass(frozen=True)
class DistributionSettings:
    bias_f: float = 0.0
    spread_inflation: float = 1.0
    bandwidth_f: float | None = None  # None = automatic
    min_valid_hours: int = 8
    obs_tolerance_f: float = 0.5


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
    open_meteo_key: str | None = None
    open_meteo_min_interval: float = 0.5

    def station(self, code: str) -> Station:
        try:
            return self.stations[code.upper()]
        except KeyError:
            raise ConfigError(f"unknown station {code!r}; configured: {', '.join(sorted(self.stations))}") from None


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy config.example.yaml to {path.name} and edit it.")
    raw = yaml.safe_load(path.read_text()) or {}
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
        )
        for m in raw.get("models") or []
    ]
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
        open_meteo_key=om.get("api_key") or None,
        open_meteo_min_interval=float(om.get("min_interval_sec", 0.5)),
    )
