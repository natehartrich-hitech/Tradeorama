import pytest
from fastapi.testclient import TestClient

from tradeorama.app import create_app
from tradeorama.config import Settings
from tradeorama.markets import normalize_market
from tradeorama.trader import OrderRejected, Trader, check_limit_order, check_market_order

# Well-known Hardhat test key; never holds real funds.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def make_settings(**overrides) -> Settings:
    base = dict(
        private_key=TEST_KEY,
        signature_type=0,
        funder_address=None,
        api_key=None,
        api_secret=None,
        api_passphrase=None,
        dry_run=True,
        max_order_usd=50.0,
    )
    base.update(overrides)
    return Settings(**base)


class FakeMarkets:
    def top_markets(self, limit=25, offset=0):
        return [{"id": "1", "question": "Top?"}]

    def search(self, query, limit=25):
        return [{"id": "2", "question": f"Search {query}?"}]

    def market(self, market_id):
        return {"id": market_id}

    def positions(self, user):
        return [{"user": user}]


@pytest.fixture
def client():
    settings = make_settings()
    return TestClient(create_app(settings, Trader(settings), FakeMarkets()))


def test_normalize_market_parses_json_string_fields():
    m = normalize_market(
        {
            "id": "9",
            "question": "Will it rain?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.62", "0.38"]',
            "clobTokenIds": '["111", "222"]',
            "volume24hr": 1234.5,
            "negRisk": False,
        }
    )
    assert m["outcomes"] == [
        {"name": "Yes", "price": 0.62, "token_id": "111"},
        {"name": "No", "price": 0.38, "token_id": "222"},
    ]
    assert m["volume_24h"] == 1234.5


@pytest.mark.parametrize("price,size", [(0, 10), (1, 10), (1.2, 1), (0.5, 0), (0.5, -1)])
def test_limit_order_rejects_invalid(price, size):
    with pytest.raises(OrderRejected):
        check_limit_order(price, size, 50)


def test_limit_order_rejects_over_max_notional():
    with pytest.raises(OrderRejected, match="MAX_ORDER_USD"):
        check_limit_order(0.5, 101, 50)
    assert check_limit_order(0.5, 100, 50) == 50


def test_market_order_limits():
    with pytest.raises(OrderRejected):
        check_market_order("BUY", 0, 50)
    with pytest.raises(OrderRejected):
        check_market_order("SELL", 51, 50)
    assert check_market_order("BUY", 20, 50) == 20


def test_status_reports_dry_run_and_address(client):
    body = client.get("/api/status").json()
    assert body["trading_enabled"] is True
    assert body["dry_run"] is True
    assert body["address"] == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def test_status_read_only_without_key():
    settings = make_settings(private_key=None)
    c = TestClient(create_app(settings, Trader(settings), FakeMarkets()))
    assert c.get("/api/status").json()["trading_enabled"] is False
    assert c.get("/api/positions").status_code == 403
    assert c.get("/api/orders").status_code == 403


def test_markets_routes(client):
    assert client.get("/api/markets").json()[0]["question"] == "Top?"
    assert client.get("/api/markets", params={"q": "btc"}).json()[0]["question"] == "Search btc?"


def test_dry_run_limit_order_never_touches_exchange(client, monkeypatch):
    monkeypatch.setattr(Trader, "client", property(lambda self: pytest.fail("exchange client used in dry run")))
    r = client.post("/api/orders", json={"token_id": "111", "side": "BUY", "price": 0.4, "size": 25})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["order"]["notional_usd"] == 10.0


def test_dry_run_market_order(client):
    r = client.post("/api/orders", json={"token_id": "111", "side": "SELL", "kind": "market", "amount": 5})
    assert r.json()["order"]["amount_unit"] == "shares"


def test_order_validation_errors(client):
    assert client.post("/api/orders", json={"token_id": "1", "side": "BUY", "price": 0.5}).status_code == 400
    assert client.post("/api/orders", json={"token_id": "1", "side": "BUY", "price": 0.9, "size": 100}).status_code == 400
    assert client.post("/api/orders", json={"token_id": "1", "side": "BUY", "kind": "market"}).status_code == 400


def test_live_limit_order_signs_and_posts(monkeypatch):
    settings = make_settings(dry_run=False)
    trader = Trader(settings)
    calls = {}

    class FakeClob:
        def create_order(self, args):
            calls["args"] = args
            return "signed"

        def post_order(self, order, order_type):
            calls["post"] = (order, order_type)
            return {"success": True, "orderID": "abc"}

    monkeypatch.setattr(Trader, "client", property(lambda self: FakeClob()))
    res = trader.place_limit_order("111", "BUY", 0.25, 10)
    assert res["response"]["orderID"] == "abc"
    assert calls["args"].price == 0.25 and calls["args"].side == "BUY"
    assert calls["post"] == ("signed", "GTC")


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Tradeorama" in r.text
