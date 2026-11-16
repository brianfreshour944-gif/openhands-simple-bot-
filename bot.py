#!/usr/bin/env python3
"""Simple crypto limit-buy bot.

Places GTC limit buy orders at the current market price for BTC, ETH and SOL.

Safety model (fail-closed):
- The Alpaca base URL is forced to the paper endpoint unless ALLOW_LIVE=1 is set.
- DRY_RUN defaults to on: orders are printed, not sent.
- Orders are only sent when Alpaca API keys are present.
"""

import logging
import math
import os
from dataclasses import dataclass

import requests

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{symbol}/spot"

TARGETS = ("BTC/USD", "ETH/USD", "SOL/USD")
COINBASE_SYMBOLS = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD"}
QTY_PRECISION = {"BTC/USD": 6, "ETH/USD": 4, "SOL/USD": 2}

log = logging.getLogger("bot")


@dataclass
class Config:
    allow_live: bool
    dry_run: bool
    notional: float
    api_key: str
    api_secret: str
    base_url: str


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    allow_live = env.get("ALLOW_LIVE", "0") == "1"
    dry_run = env.get("DRY_RUN", "1") == "1"
    notional = float(env.get("NOTIONAL_USD", "100"))
    api_key = env.get("ALPACA_API_KEY", "")
    api_secret = env.get("ALPACA_SECRET_KEY", "")
    base_url = env.get("ALPACA_BASE_URL", PAPER_BASE_URL)
    if not allow_live:
        base_url = PAPER_BASE_URL
    return Config(allow_live, dry_run, notional, api_key, api_secret, base_url)


def fetch_price(session, symbol) -> float:
    url = COINBASE_SPOT_URL.format(symbol=COINBASE_SYMBOLS[symbol])
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return float(resp.json()["data"]["amount"])


def order_qty(notional, price, precision) -> float:
    """Fractional qty, rounded DOWN so the notional is never exceeded."""
    factor = 10 ** precision
    return math.floor(notional / price * factor) / factor


def build_order(symbol, qty, price) -> dict:
    return {
        "symbol": symbol,
        "qty": f"{qty}",
        "side": "buy",
        "type": "limit",
        "time_in_force": "gtc",
        "limit_price": f"{price:.2f}",
    }


def send_order(session, config, order) -> dict:
    if config.dry_run:
        log.info("DRY RUN - would send order: %s", order)
        return {"dry_run": True, "order": order}
    if not config.api_key or not config.api_secret:
        raise RuntimeError("Alpaca API keys missing; cannot send orders")
    headers = {
        "APCA-API-KEY-ID": config.api_key,
        "APCA-API-SECRET-KEY": config.api_secret,
    }
    resp = session.post(
        f"{config.base_url}/v2/orders", json=order, headers=headers, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def run(config) -> list:
    if config.base_url != PAPER_BASE_URL and not config.allow_live:
        raise RuntimeError("Live endpoint blocked: set ALLOW_LIVE=1 to enable")
    if not config.dry_run and (not config.api_key or not config.api_secret):
        raise RuntimeError("Alpaca API keys missing; cannot send orders")
    results = []
    with requests.Session() as session:
        for symbol in TARGETS:
            price = fetch_price(session, symbol)
            qty = order_qty(config.notional, price, QTY_PRECISION[symbol])
            order = build_order(symbol, qty, price)
            result = send_order(session, config, order)
            log.info("%s: price %.2f, qty %s -> %s", symbol, price, qty, result)
            results.append({"symbol": symbol, "price": price, "qty": qty, "result": result})
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    log.info(
        "mode=%s base_url=%s notional=$%.2f targets=%s",
        "DRY-RUN" if config.dry_run else "LIVE-PAPER",
        config.base_url,
        config.notional,
        ",".join(TARGETS),
    )
    run(config)


if __name__ == "__main__":
    main()
