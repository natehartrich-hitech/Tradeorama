"""Settings loaded from a YAML file."""

from dataclasses import dataclass, field, fields, replace
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
    pmus_event_prefix: str | None = None  # Polymarket US daily-HIGH event slug prefix, e.g. "temp-mdwhigh"
    pmus_low_prefix: str | None = None  # daily-LOW prefix, e.g. "temp-mdwlow"
    # Per-city calibration, from the station's own `distribution` / `distribution_low` and
    # `weights` / `weights_low` sections: {kind: {setting: value}} and {kind: {model id: weight}}.
    overrides: dict = field(default_factory=dict, hash=False, compare=False)
    weight_overrides: dict = field(default_factory=dict, hash=False, compare=False)

    def prefix(self, kind: str) -> str | None:
        return self.pmus_event_prefix if kind == "high" else self.pmus_low_prefix

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
    same_day_last_hour: float = 12.0  # no trades on TODAY's high market from this hour, local standard time
    same_day_last_hour_low: float = 6.0  # same for today's LOW market: by dawn the low is mostly in
    kinds: tuple[str, ...] = ("high", "low")  # which markets paper trades

    def last_hour(self, kind: str) -> float:
        return self.same_day_last_hour if kind == "high" else self.same_day_last_hour_low


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
    distribution_low: DistributionSettings | None = None
    heartbeat_url: str | None = None  # dead man's switch pinged by `paper` (e.g. a Healthchecks.io check URL)

    def distribution_for(self, kind: str, station: Station | None = None) -> DistributionSettings:
        """Settings in layers: `distribution`, then `distribution_low` for lows, then the city's own
        `distribution` / `distribution_low` section, so each city and kind can be calibrated alone."""
        base = self.distribution_low if kind == "low" and self.distribution_low else self.distribution
        own = station.overrides.get(kind) if station else None
        return replace(base, **own) if own else base

    def weights_for(self, kind: str, station: Station | None = None) -> dict[str, float]:
        """Model weights, with the city's own `weights` / `weights_low` on top (0 turns a model off there)."""
        weights = {m.id: m.weight for m in self.models}
        if station:
            weights.update(station.weight_overrides.get(kind) or {})
        return weights

    def station(self, code: str) -> Station:
        try:
            return self.stations[code.upper()]
        except KeyError:
            raise ConfigError(f"unknown station {code!r}; configured: {', '.join(sorted(self.stations))}") from None


DISTRIBUTION_KEYS = {f.name for f in fields(DistributionSettings)}


def distribution_values(raw: dict, where: str) -> dict:
    """Validated, typed DistributionSettings fields from a config section."""
    unknown = set(raw) - DISTRIBUTION_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(DISTRIBUTION_KEYS))}")
    out = {}
    for k, v in raw.items():
        if k == "bandwidth_f":
            out[k] = None if v in (None, "auto") else float(v)
        elif k == "min_valid_hours":
            out[k] = int(v)
        else:
            out[k] = float(v)
    if out.get("spread_inflation", 1.0) <= 0:
        raise ConfigError(f"{where}: spread_inflation must be positive")
    return out


def parse_paper(raw: dict) -> PaperSettings:
    kinds = tuple(raw.get("kinds") or ("high", "low"))
    if not set(kinds) <= {"high", "low"}:
        raise ConfigError("paper.kinds must be a list of high and/or low")
    return PaperSettings(kinds=kinds, **{k: float(v) for k, v in raw.items() if k != "kinds"})


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
                pmus_low_prefix=s.get("pmus_low_prefix"),
                overrides={kind: distribution_values(s[key], f"station {code} {key}")
                           for kind, key in (("high", "distribution"), ("low", "distribution_low")) if s.get(key)},
                weight_overrides={kind: {str(m): float(w) for m, w in s[key].items()}
                                  for kind, key in (("high", "weights"), ("low", "weights_low")) if s.get(key)},
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
    ids = {m.id for m in models}
    for st in stations.values():
        for kind, weights in st.weight_overrides.items():
            bad = set(weights) - ids
            if bad:
                raise ConfigError(f"station {st.code} weights{'' if kind == 'high' else '_low'}: unknown model(s) {', '.join(sorted(bad))}")
            merged = {m.id: m.weight for m in models} | weights
            if any(w < 0 for w in merged.values()) or sum(merged.values()) <= 0:
                raise ConfigError(f"station {st.code}: model weights must be non-negative and not all zero")

    d = distribution_values(raw.get("distribution") or {}, "distribution")
    dist = DistributionSettings(**d)
    dist_low = None
    if raw.get("distribution_low"):
        dist_low = replace(dist, **distribution_values(raw["distribution_low"], "distribution_low"))

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
        paper=parse_paper(raw.get("paper") or {}),
        distribution_low=dist_low,
        heartbeat_url=str(raw.get("heartbeat_url") or "").strip() or None,
    )
