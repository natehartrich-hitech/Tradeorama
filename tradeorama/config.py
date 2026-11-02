import os
from dataclasses import dataclass

from dotenv import load_dotenv

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"
POLYGON_CHAIN_ID = 137


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    private_key: str | None
    signature_type: int
    funder_address: str | None
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    dry_run: bool
    max_order_usd: float

    @property
    def can_trade(self) -> bool:
        return bool(self.private_key)

    @property
    def has_api_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    return Settings(
        private_key=env("POLY_PRIVATE_KEY") or None,
        signature_type=int(env("POLY_SIGNATURE_TYPE") or 1),
        funder_address=env("POLY_FUNDER_ADDRESS") or None,
        api_key=env("POLY_API_KEY") or None,
        api_secret=env("POLY_API_SECRET") or None,
        api_passphrase=env("POLY_API_PASSPHRASE") or None,
        # Default to dry-run so a fresh install can never place a real order by accident.
        dry_run=_bool(env("DRY_RUN"), True),
        max_order_usd=float(env("MAX_ORDER_USD") or 50),
    )


@dataclass(frozen=True)
class StrategySettings:
    odds_api_key: str | None
    sports: list[str]
    bankroll_usd: float
    kelly_multiplier: float
    min_edge: float
    max_game_exposure_usd: float
    max_daily_spend_usd: float
    max_odds_age_sec: float
    min_minutes_before_start: float
    max_divergence: float
    db_path: str


def load_strategy_settings() -> StrategySettings:
    load_dotenv()
    env = os.environ.get
    return StrategySettings(
        odds_api_key=env("ODDS_API_KEY") or None,
        sports=[s.strip() for s in (env("STRATEGY_SPORTS") or "basketball_nba").split(",") if s.strip()],
        bankroll_usd=float(env("BANKROLL_USD") or 200),
        kelly_multiplier=float(env("KELLY_MULTIPLIER") or 0.25),
        min_edge=float(env("MIN_EDGE") or 0.03),
        max_game_exposure_usd=float(env("MAX_GAME_EXPOSURE_USD") or 25),
        max_daily_spend_usd=float(env("MAX_DAILY_SPEND_USD") or 100),
        max_odds_age_sec=float(env("MAX_ODDS_AGE_SEC") or 300),
        min_minutes_before_start=float(env("MIN_MINUTES_BEFORE_START") or 5),
        max_divergence=float(env("MAX_DIVERGENCE") or 0.15),
        db_path=env("STRATEGY_DB") or "tradeorama.db",
    )
