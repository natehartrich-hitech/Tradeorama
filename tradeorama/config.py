import os
import re
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


def _clean(value: str | None) -> str | None:
    # Tolerate stray spaces and quotes pasted in along with a key or address.
    value = (value or "").strip().strip("'\"").strip()
    return value or None


HEX_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def setup_problems(s: Settings) -> list[str]:
    """Plain-language problems with the wallet settings. Never includes the key itself."""
    problems = []
    if s.private_key is None:
        return problems  # read-only mode is a valid setup
    key = s.private_key
    if "your" in key.lower() or "here" in key.lower():
        problems.append("POLY_PRIVATE_KEY is still the example text from .env.example. Replace it with your real key.")
    elif not HEX_KEY.match(key):
        digits = len(key[2:] if key.lower().startswith("0x") else key)
        problems.append(
            "POLY_PRIVATE_KEY doesn't look like a private key. It should be 64 characters of 0-9 and a-f, "
            f"optionally starting with 0x; this one has {digits} characters after any 0x"
            + (" and contains other characters." if not re.fullmatch(r"(0x)?[0-9a-fA-F]*", key) else ".")
        )
    if s.signature_type not in (0, 1, 2):
        problems.append("POLY_SIGNATURE_TYPE must be 0, 1 or 2 (1 for email login, 2 for MetaMask).")
    if s.funder_address is None:
        if s.signature_type in (1, 2):
            problems.append(
                "POLY_FUNDER_ADDRESS is empty. Copy the 0x… address from your Polymarket profile "
                "(it may only appear after your first deposit)."
            )
    elif "your" in s.funder_address.lower():
        problems.append("POLY_FUNDER_ADDRESS is still the example text. Replace it with your Polymarket profile address.")
    elif not HEX_ADDRESS.match(s.funder_address):
        problems.append("POLY_FUNDER_ADDRESS should be 0x followed by 40 characters of 0-9 and a-f.")
    return problems


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    sig = _clean(env("POLY_SIGNATURE_TYPE")) or "1"
    return Settings(
        private_key=_clean(env("POLY_PRIVATE_KEY")),
        signature_type=int(sig) if sig.isdigit() else -1,
        funder_address=_clean(env("POLY_FUNDER_ADDRESS")),
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
    poll_near_hours: float = 2
    poll_mid_hours: float = 12
    poll_mid_sec: float = 900
    poll_far_sec: float = 3600
    poll_idle_sec: float = 6 * 3600


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
        poll_near_hours=float(env("POLL_NEAR_HOURS") or 2),
        poll_mid_hours=float(env("POLL_MID_HOURS") or 12),
        poll_mid_sec=float(env("POLL_MID_SEC") or 900),
        poll_far_sec=float(env("POLL_FAR_SEC") or 3600),
        poll_idle_sec=float(env("POLL_IDLE_SEC") or 6 * 3600),
    )
