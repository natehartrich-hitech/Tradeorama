"""Order placement on Polymarket's CLOB, with dry-run and per-order risk limits."""

import dataclasses
import threading
from typing import Literal

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from .config import CLOB_HOST, POLYGON_CHAIN_ID, Settings

Side = Literal["BUY", "SELL"]


class OrderRejected(ValueError):
    """Raised when an order fails local validation before reaching the exchange."""


class TradingDisabled(RuntimeError):
    """Raised when a trading call is made without a configured private key."""


def check_limit_order(price: float, size: float, max_order_usd: float) -> float:
    if not 0 < price < 1:
        raise OrderRejected("price must be between 0 and 1 (exclusive)")
    if size <= 0:
        raise OrderRejected("size must be positive")
    notional = price * size
    if notional > max_order_usd:
        raise OrderRejected(f"order notional ${notional:.2f} exceeds MAX_ORDER_USD ${max_order_usd:.2f}")
    return notional


def check_market_order(side: Side, amount: float, max_order_usd: float) -> float:
    if amount <= 0:
        raise OrderRejected("amount must be positive")
    # BUY amounts are USDC; SELL amounts are shares, each worth at most $1.
    # Either way `amount` is an upper bound on the notional.
    if amount > max_order_usd:
        raise OrderRejected(f"order notional up to ${amount:.2f} exceeds MAX_ORDER_USD ${max_order_usd:.2f}")
    return amount


class Trader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.public = ClobClient(CLOB_HOST)
        self._auth: ClobClient | None = None
        self._lock = threading.Lock()

    # --- public data -------------------------------------------------------

    def order_book(self, token_id: str) -> dict:
        return dataclasses.asdict(self.public.get_order_book(token_id))

    def midpoint(self, token_id: str) -> dict:
        return self.public.get_midpoint(token_id)

    # --- authenticated -----------------------------------------------------

    @property
    def client(self) -> ClobClient:
        if not self.settings.can_trade:
            raise TradingDisabled("POLY_PRIVATE_KEY is not set; the app is in read-only mode")
        with self._lock:
            if self._auth is None:
                s = self.settings
                client = ClobClient(
                    CLOB_HOST,
                    chain_id=POLYGON_CHAIN_ID,
                    key=s.private_key,
                    signature_type=s.signature_type,
                    funder=s.funder_address,
                )
                if s.has_api_creds:
                    creds = ApiCreds(s.api_key, s.api_secret, s.api_passphrase)
                else:
                    creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
                self._auth = client
            return self._auth

    def address(self) -> str | None:
        if not self.settings.can_trade:
            return None
        # Funds live at the funder (proxy) address when one is configured.
        return self.settings.funder_address or Account.from_key(self.settings.private_key).address

    def place_limit_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        order_type: Literal["GTC", "FOK", "FAK"] = "GTC",
    ) -> dict:
        notional = check_limit_order(price, size, self.settings.max_order_usd)
        summary = {
            "kind": "limit",
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "order_type": order_type,
            "notional_usd": round(notional, 4),
        }
        if self.settings.dry_run:
            return {"dry_run": True, "order": summary}
        args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY if side == "BUY" else SELL)
        signed = self.client.create_order(args)
        resp = self.client.post_order(signed, getattr(OrderType, order_type))
        return {"dry_run": False, "order": summary, "response": resp}

    def place_market_order(self, token_id: str, side: Side, amount: float) -> dict:
        check_market_order(side, amount, self.settings.max_order_usd)
        summary = {
            "kind": "market",
            "token_id": token_id,
            "side": side,
            "amount": amount,
            "amount_unit": "USDC" if side == "BUY" else "shares",
            "order_type": "FOK",
        }
        if self.settings.dry_run:
            return {"dry_run": True, "order": summary}
        args = MarketOrderArgs(token_id=token_id, amount=amount, side=BUY if side == "BUY" else SELL)
        signed = self.client.create_market_order(args)
        resp = self.client.post_order(signed, OrderType.FOK)
        return {"dry_run": False, "order": summary, "response": resp}

    def open_orders(self, token_id: str | None = None) -> list:
        params = OpenOrderParams(asset_id=token_id) if token_id else None
        return self.client.get_orders(params)

    def cancel(self, order_id: str) -> dict:
        if self.settings.dry_run:
            return {"dry_run": True, "cancelled": [order_id]}
        return self.client.cancel(order_id)

    def cancel_all(self) -> dict:
        if self.settings.dry_run:
            return {"dry_run": True, "cancelled": "all"}
        return self.client.cancel_all()
