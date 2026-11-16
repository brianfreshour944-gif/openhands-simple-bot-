import math

import pytest
import requests

import bot


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    """Test double for the HTTP layer only; business logic runs for real."""

    def __init__(self, get_payload=None, post_payload=None, get_status=200):
        self.get_payload = get_payload
        self.post_payload = post_payload
        self.get_status = get_status
        self.get_calls = []
        self.post_calls = []

    def get(self, url, timeout=None):
        self.get_calls.append((url, timeout))
        return FakeResponse(self.get_payload, status=self.get_status)

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append((url, json, headers, timeout))
        return FakeResponse(self.post_payload)


def coinbase_payload(price):
    return {"data": {"base": "BTC", "currency": "USD", "amount": str(price)}}


# --- config / fail-closed guard ---

def test_default_config_is_paper_and_dry_run():
    config = bot.load_config(env={})
    assert config.base_url == bot.PAPER_BASE_URL
    assert config.dry_run is True
    assert config.notional == 100.0


def test_live_base_url_is_forced_back_to_paper_without_allow_live():
    config = bot.load_config(env={"ALPACA_BASE_URL": "https://api.alpaca.markets"})
    assert config.base_url == bot.PAPER_BASE_URL


def test_allow_live_permits_custom_base_url():
    config = bot.load_config(
        env={"ALLOW_LIVE": "1", "ALPACA_BASE_URL": "https://api.alpaca.markets"}
    )
    assert config.base_url == "https://api.alpaca.markets"


def test_run_blocks_custom_base_url_without_allow_live():
    # build Config directly: load_config already forces paper, so this
    # exercises the second-layer guard inside run() for a tampered config
    config = bot.Config(
        allow_live=False,
        dry_run=True,
        notional=100.0,
        api_key="",
        api_secret="",
        base_url="https://api.alpaca.markets",
    )
    with pytest.raises(RuntimeError, match="Live endpoint blocked"):
        bot.run(config)


def test_run_blocks_sending_without_keys():
    config = bot.load_config(env={"DRY_RUN": "0"})
    with pytest.raises(RuntimeError, match="keys missing"):
        bot.run(config)


# --- qty and order construction ---

def test_order_qty_rounds_down_never_exceeds_notional():
    qty = bot.order_qty(100.0, 71.25, 2)
    assert qty == 1.40
    assert qty * 71.25 <= 100.0


def test_order_qty_btc_precision():
    qty = bot.order_qty(100.0, 64770.12, 6)
    assert qty == 0.001543
    assert math.floor(qty * 10**6) == qty * 10**6


def test_build_order_payload_shape():
    order = bot.build_order("BTC/USD", 0.001544, 64770.12)
    assert order == {
        "symbol": "BTC/USD",
        "qty": "0.001544",
        "side": "buy",
        "type": "limit",
        "time_in_force": "gtc",
        "limit_price": "64770.12",
    }


# --- price fetching ---

def test_fetch_price_parses_coinbase_spot():
    session = FakeSession(get_payload=coinbase_payload(64770.5))
    assert bot.fetch_price(session, "BTC/USD") == 64770.5
    url, _ = session.get_calls[0]
    assert url == "https://api.coinbase.com/v2/prices/BTC-USD/spot"


def test_fetch_price_raises_on_http_error():
    session = FakeSession(get_payload={}, get_status=500)
    with pytest.raises(requests.HTTPError):
        bot.fetch_price(session, "BTC/USD")


# --- order sending ---

def test_dry_run_does_not_call_network():
    config = bot.load_config(env={})
    session = FakeSession(post_payload={})
    result = bot.send_order(session, config, bot.build_order("SOL/USD", 1.40, 71.25))
    assert result["dry_run"] is True
    assert session.post_calls == []


def test_real_send_posts_to_paper_endpoint():
    config = bot.load_config(
        env={"DRY_RUN": "0", "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}
    )
    session = FakeSession(post_payload={"id": "abc"})
    result = bot.send_order(session, config, bot.build_order("SOL/USD", 1.40, 71.25))
    assert result == {"id": "abc"}
    url, order, headers, _ = session.post_calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/orders"
    assert order["side"] == "buy"
    assert headers["APCA-API-KEY-ID"] == "k"


def test_send_order_refuses_without_keys():
    config = bot.load_config(env={"DRY_RUN": "0"})
    session = FakeSession(post_payload={})
    with pytest.raises(RuntimeError, match="keys missing"):
        bot.send_order(session, config, bot.build_order("SOL/USD", 1.40, 71.25))
    assert session.post_calls == []


# --- end-to-end run against fake transport ---

def test_run_places_all_three_orders_dry_run(monkeypatch):
    session = FakeSession()
    config = bot.load_config(env={})
    prices = {"BTC/USD": 64770.0, "ETH/USD": 1809.0, "SOL/USD": 71.25}
    monkeypatch.setattr(bot, "fetch_price", lambda s, symbol: prices[symbol])
    results = bot.run(config)
    assert [r["symbol"] for r in results] == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert [r["result"]["dry_run"] for r in results] == [True, True, True]
    assert session.post_calls == []