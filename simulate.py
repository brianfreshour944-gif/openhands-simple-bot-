"""Simulation mode: replay a fake price series through the exact bot rule logic.

No API calls, no network. Uses the same state machine (bot.step_coin) that the
live bot uses, with a fake executor that simulates market fills and the taker
fee. Verifies the rules offline (spec section 10).

Usage:
    python simulate.py                # built-in demo scenario
    python simulate.py prices.json    # custom series: {"BTC/USD": [65000, ...]}
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

import bot


class SimExecutor:
    """Simulates a market fill: quantity is sized with the taker fee applied."""

    def __init__(self, price, fee_pct):
        self.price = price
        self.fee_pct = fee_pct

    def __call__(self, symbol, side, notional_usd):
        if side == "buy":
            return bot.qty_from_notional(notional_usd, self.price, self.fee_pct)
        # Sell: proceeds are reported by the caller's math; executor just fills.
        return notional_usd / self.price  # quantity sold


def run_simulation(cfg, series, trades_path, state_path, log_path, quiet=False):
    """Replay each price series through step_coin; return the final state.

    Every poll advances the clock by one day, which exercises the underwater
    day counter as well.
    """
    log = logging.getLogger("bot")
    log.handlers.clear()
    fh = logging.FileHandler(log_path)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    log.setLevel(logging.INFO)
    if not quiet:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(sh)

    state = bot.BotState()
    for symbol in cfg["coins"]:
        state.coins[symbol] = bot.CoinState(symbol)

    symbols = [s for s in cfg["coins"] if s in series]
    if not symbols:
        raise SystemExit("series has none of the configured coins")

    for day, prices in enumerate(zip(*[series[s] for s in symbols]), start=1):
        for symbol, price in zip(symbols, prices):
            coin = state.coins[symbol]
            coin._last_underwater_day = _shift_day(coin._last_underwater_day, day)
            executor = SimExecutor(price, cfg["fee_pct_per_side"])
            before = coin.state
            bot.step_coin(cfg, coin, price, executor)
            if coin.state != before:
                log.info("day %d %s price=%.2f %s -> %s",
                         day, symbol, price, before, coin.state)
    bot.save_state(state, state_path)
    return state


def _shift_day(_last, day):
    """Give each simulated poll its own UTC date so underwater days count."""
    return (datetime(2026, 1, 1, tzinfo=timezone.utc)
            .strftime("%Y-%m-%d")) if day == 0 else f"2026-01-{day:02d}"


def default_series():
    """Built-in scenario that exercises every rule: entry dip, +10% add-on,
    +20% sell, a long underwater stretch, and a second cycle."""
    return {
        # BTC: dip to entry -> buy; rise -> add-on; hit target -> sell;
        # dip again -> new cycle buy.
        "BTC/USD": [70000, 64000, 65125, 71500, 78000, 78000, 64000, 78000],
        # ETH: enters at 1800, drifts underwater for several days, recovers,
        # then hits target.
        "ETH/USD": [1900, 1800, 1810, 1780, 1750, 1720, 2260, 2260],
        # SOL: enters at 70, rises straight to target without the add-on ever
        # being missed (sell is checked first in HOLDING_1).
        "SOL/USD": [75, 70, 80, 90, 95, 95, 70, 95],
    }


def main():
    cfg = bot.load_config()
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            series = json.load(f)
    else:
        series = default_series()
    state = run_simulation(
        cfg, series,
        trades_path="trades.csv",
        state_path="state.json",
        log_path="bot.log",
    )
    print("\n=== simulation complete ===")
    for symbol, coin in state.coins.items():
        print(f"{symbol}: state={coin.state} cycles={coin.cycle_count} "
              f"profit=${coin.cumulative_profit:.4f} "
              f"underwater_days={coin.underwater_days:.0f}")
    print(f"Trades written to trades.csv; state in state.json; log in bot.log")


if __name__ == "__main__":
    main()
