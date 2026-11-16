"""Simple rules-based paper trading bot for crypto via Alpaca PAPER API.

Rules (see README.md for full spec):
- Each coin runs an independent state machine:
  WAITING -> HOLDING_1 -> HOLDING_2 -> DONE_CYCLE -> WAITING ...
- BUY 1 (WAITING): price <= entry * (1 + entry_tolerance_pct/100).
- BUY 2 (HOLDING_1): price >= avg_entry * (1 + add_trigger_pct/100).
- SELL (HOLDING_1 or HOLDING_2): price >= avg_entry * (1 + sell_target_pct/100).
- No stop loss, no shorting, no indicators, no AI. Sell is always the whole position.
- Market orders, time_in_force="gtc", fee 0.25% per side (taker).

Run with:  python bot.py            (live paper-trading poll loop)
           python bot.py --simulate (offline replay, no API calls)
"""

import csv
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from alpaca.common.enums import BaseURL
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest

# Alpaca's published minimum notional for crypto orders.
ALPACA_MIN_CRYPTO_ORDER_USD = 1.0

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
TRADES_PATH = "trades.csv"
LOG_PATH = "bot.log"

log = logging.getLogger("bot")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Config and state
# ---------------------------------------------------------------------------

def load_config(path=CONFIG_PATH):
    """Read config.json fresh on every startup (spec section 3)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class CoinState:
    """Per-coin state machine state (spec section 4)."""
    symbol: str
    state: str = "WAITING"
    qty: float = 0.0              # quantity currently held
    dollars_spent: float = 0.0    # total dollars spent this cycle (incl. fees)
    avg_entry: float = 0.0        # dollars spent / qty received
    cycle_count: int = 0          # completed sell cycles
    cumulative_profit: float = 0.0
    underwater_days: float = 0.0  # days price has been below avg_entry this cycle
    _last_underwater_day: str = ""
    buy_retry_count: int = 0      # consecutive failed buys (spec section 8)

    def to_dict(self):
        return {
            "state": self.state,
            "qty": self.qty,
            "dollars_spent": self.dollars_spent,
            "avg_entry": self.avg_entry,
            "cycle_count": self.cycle_count,
            "cumulative_profit": self.cumulative_profit,
            "underwater_days": self.underwater_days,
        }

    @classmethod
    def from_dict(cls, symbol, d):
        return cls(
            symbol=symbol,
            state=d.get("state", "WAITING"),
            qty=d.get("qty", 0.0),
            dollars_spent=d.get("dollars_spent", 0.0),
            avg_entry=d.get("avg_entry", 0.0),
            cycle_count=d.get("cycle_count", 0),
            cumulative_profit=d.get("cumulative_profit", 0.0),
            underwater_days=d.get("underwater_days", 0.0),
        )


@dataclass
class BotState:
    coins: dict = field(default_factory=dict)  # symbol -> CoinState

    def to_dict(self):
        return {"coins": {s: c.to_dict() for s, c in self.coins.items()}}


def load_state(cfg, path=None):
    """Load state.json; fall back to a fresh state (spec section 7)."""
    path = path or STATE_PATH
    state = BotState()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for symbol in cfg["coins"]:
            d = data.get("coins", {}).get(symbol, {})
            state.coins[symbol] = CoinState.from_dict(symbol, d)
        log.info("Loaded state from %s", path)
    else:
        for symbol in cfg["coins"]:
            state.coins[symbol] = CoinState(symbol)
        log.info("No state file found; starting fresh.")
    return state


def save_state(state, path=None):
    """Persist state after every action (spec section 7)."""
    path = path or STATE_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.handlers.clear()
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)


TRADE_FIELDS = [
    "timestamp", "symbol", "side", "quantity", "price", "fee_estimate",
    "state_before", "state_after", "net_profit",
]


def append_trade(symbol, side, quantity, price, fee, state_before, state_after,
                 net_profit="", trades_path=None):
    """Append one order to trades.csv (spec section 7)."""
    trades_path = trades_path or TRADES_PATH
    new_file = not os.path.exists(trades_path)
    with open(trades_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({
            "timestamp": now_iso(),
            "symbol": symbol,
            "side": side,
            "quantity": f"{quantity:.10f}",
            "price": f"{price:.2f}",
            "fee_estimate": f"{fee:.4f}",
            "state_before": state_before,
            "state_after": state_after,
            "net_profit": "" if net_profit == "" else f"{net_profit:.4f}",
        })


# ---------------------------------------------------------------------------
# Fee / price math (spec section 6)
# ---------------------------------------------------------------------------

def qty_from_notional(notional_usd, price, fee_pct):
    """Quantity received after paying the taker fee on a buy."""
    return notional_usd / price / (1 + fee_pct / 100)


def proceeds_from_qty(qty, price, fee_pct):
    """Dollars received after the taker fee on a sell."""
    return qty * price * (1 - fee_pct / 100)


def buy_trigger_price(cfg, symbol):
    # Rule 1: buy when price is at or below entry * (1 + tolerance).
    # Tiny epsilon so an exact boundary price is never missed to float error.
    return cfg["entry_price"][symbol] * (1 + cfg["entry_tolerance_pct"] / 100)


def add_trigger_price(cfg, coin):
    # Rule 2: add-on buy when price is add_trigger_pct above average entry.
    return coin.avg_entry * (1 + cfg["add_trigger_pct"] / 100)


def sell_trigger_price(cfg, coin):
    # Rules 2/3: sell when price is sell_target_pct above average entry.
    return coin.avg_entry * (1 + cfg["sell_target_pct"] / 100)


def breakeven_price(cfg, coin):
    # Break-even is about 0.5% above average entry (two 0.25% fees).
    fee = cfg["fee_pct_per_side"]
    return coin.avg_entry * (1 + fee / 100) / (1 - fee / 100)


# ---------------------------------------------------------------------------
# Alpaca clients (PAPER ONLY)
# ---------------------------------------------------------------------------

def make_clients():
    """Build clients; refuse to run unless the PAPER endpoint is in use."""
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in environment.")
        sys.exit(1)

    trading = TradingClient(api_key, secret_key, paper=True)
    # Startup check: refuse to run if paper mode is not enabled. alpaca-py
    # exposes the endpoint via _base_url (TRADING_PAPER vs TRADING_LIVE).
    if str(trading._base_url) != str(BaseURL.TRADING_PAPER):
        log.error("PAPER mode not enabled on the trading client - refusing to run.")
        sys.exit(1)

    data = CryptoHistoricalDataClient(api_key, secret_key)
    return trading, data


def fetch_prices(data_client, symbols):
    """Latest bid price per symbol, from the top-of-book quote."""
    req = CryptoLatestQuoteRequest(symbol_or_symbols=list(symbols))
    quotes = data_client.get_crypto_latest_quote(req)
    prices = {}
    for s in symbols:
        q = quotes[s]
        px = getattr(q, "bid_price", None) or getattr(q, "ask_price", None)
        if px is None or px <= 0:
            raise ValueError(f"No usable quote for {s}")
        prices[s] = float(px)
    return prices


def submit_market_order(trading, symbol, side, notional_usd):
    """Market order, notional (USD) sized, GTC (spec section 2)."""
    order = MarketOrderRequest(
        symbol=symbol,
        notional=round(notional_usd, 2),
        side=side,
        type="market",
        time_in_force="gtc",
    )
    return trading.submit_order(order)


# ---------------------------------------------------------------------------
# The trading rules - one step of the per-coin state machine
# ---------------------------------------------------------------------------

def record_underwater_day(cfg, coin, price, today=None):
    """Count calendar days underwater (price below avg entry) - spec 7."""
    if coin.avg_entry > 0 and price < coin.avg_entry:
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if coin._last_underwater_day != today:
            coin.underwater_days += 1
            coin._last_underwater_day = today
            log.info("%s underwater day %d (price %.2f < avg %.2f)",
                     coin.symbol, int(coin.underwater_days), price, coin.avg_entry)


def step_coin(cfg, coin, price, place_order, today=None):
    """Advance one coin's state machine by one poll.

    `place_order(symbol, side, notional_usd)` executes the real order in live
    mode and simulates fills in simulation mode. The rule logic is identical
    in both modes; only the executor differs (spec section 10).

    An order failure leaves the state unchanged; the next poll retries
    (spec section 8). State is only mutated after the order succeeds.
    """
    fee_pct = cfg["fee_pct_per_side"]
    before = coin.state

    record_underwater_day(cfg, coin, price, today)

    # Rules 2/3 (HOLDING_1, HOLDING_2): check the sell target first so a fast
    # run to target is not missed in HOLDING_1.
    if coin.state in ("HOLDING_1", "HOLDING_2"):
        # Epsilon keeps the "at or above" boundary exact under float error.
        if price >= sell_trigger_price(cfg, coin) * (1 - 1e-9):
            if price < breakeven_price(cfg, coin):
                # Safety check: never sell below break-even (spec 6).
                log.warning("%s sell target hit but below break-even; skipping.",
                            coin.symbol)
            else:
                proceeds = proceeds_from_qty(coin.qty, price, fee_pct)
                fee = coin.qty * price * fee_pct / 100
                net_profit = proceeds - coin.dollars_spent
                try:
                    place_order(coin.symbol, "sell", proceeds)  # whole position
                except Exception as e:
                    log.error("%s sell failed; will retry next poll: %s",
                              coin.symbol, e)
                    return
                log.info("%s SELL qty=%.10f price=%.2f proceeds=%.2f profit=%.4f",
                         coin.symbol, coin.qty, price, proceeds, net_profit)
                append_trade(coin.symbol, "sell", coin.qty, price, fee,
                             before, "DONE_CYCLE", net_profit)
                # Rule 4: record profit, reset for the next cycle.
                coin.cumulative_profit += net_profit
                coin.cycle_count += 1
                coin.qty = 0.0
                coin.dollars_spent = 0.0
                coin.avg_entry = 0.0
                coin.underwater_days = 0.0
                coin._last_underwater_day = ""
                coin.state = "DONE_CYCLE"
                return

    # Rule 1 (WAITING): first buy at or below entry (with tolerance).
    if coin.state == "WAITING":
        # Epsilon keeps the "at or below" boundary exact under float error.
        if price <= buy_trigger_price(cfg, coin.symbol) * (1 + 1e-9):
            notional = cfg["first_buy_usd"][coin.symbol]
            try:
                qty = place_order(coin.symbol, "buy", notional)
            except Exception as e:
                # Spec 8: consecutive failed buys are capped with a warning
                # by the caller; here the state simply stays WAITING.
                coin.buy_retry_count += 1
                log.error("%s buy 1 failed (attempt %d): %s",
                          coin.symbol, coin.buy_retry_count, e)
                if coin.buy_retry_count >= 3:
                    log.warning("%s buy failed %d times in a row - please "
                                "check the account or config.",
                                coin.symbol, coin.buy_retry_count)
                return
            fee = notional * fee_pct / 100
            coin.qty += qty
            coin.dollars_spent += notional
            coin.avg_entry = coin.dollars_spent / coin.qty
            coin.state = "HOLDING_1"
            coin.buy_retry_count = 0
            log.info("%s BUY1 qty=%.10f price=%.2f notional=%.2f avg=%.2f",
                     coin.symbol, qty, price, notional, coin.avg_entry)
            append_trade(coin.symbol, "buy", qty, price, fee, before, coin.state)
            return

    # Rule 2 (HOLDING_1): add-on buy after price rises add_trigger_pct above
    # the average entry. Epsilon keeps the boundary exact under float error.
    if coin.state == "HOLDING_1" and price >= add_trigger_price(cfg, coin) * (1 - 1e-9):
        notional = cfg["second_buy_usd"][coin.symbol]
        try:
            qty = place_order(coin.symbol, "buy", notional)
        except Exception as e:
            # Spec 8: log and retry next poll; state stays HOLDING_1.
            log.error("%s buy 2 failed; will retry next poll: %s", coin.symbol, e)
            return
        fee = notional * fee_pct / 100
        coin.qty += qty
        coin.dollars_spent += notional
        coin.avg_entry = coin.dollars_spent / coin.qty
        coin.state = "HOLDING_2"
        log.info("%s BUY2 qty=%.10f price=%.2f notional=%.2f avg=%.2f",
                 coin.symbol, qty, price, notional, coin.avg_entry)
        append_trade(coin.symbol, "buy", qty, price, fee, before, coin.state)
        return

    # Rule 3 (HOLDING_2): do nothing except the sell check above.
    # Rule 4 (DONE_CYCLE): flip back to WAITING. A new buy can only happen when
    # price is again at or below the entry price - which is exactly the
    # WAITING rule - so DONE_CYCLE has nothing else to do.
    if coin.state == "DONE_CYCLE":
        coin.state = "WAITING"
        log.info("%s cycle complete; profit so far %.4f; back to WAITING.",
                 coin.symbol, coin.cumulative_profit)


# ---------------------------------------------------------------------------
# Live order execution and poll loop
# ---------------------------------------------------------------------------

class LiveExecutor:
    """Places real (paper) orders and reports the estimated filled quantity.

    Orders are submitted with a notional, but the accounting needs quantity,
    so if the broker has not reported a fill yet we estimate it from the
    submitted order's average fill price with fees applied (spec section 6).
    """

    def __init__(self, trading, cfg, price, min_order_usd):
        self.trading = trading
        self.cfg = cfg
        self.price = price
        self.min_order_usd = min_order_usd

    def __call__(self, symbol, side, notional_usd):
        if notional_usd < self.min_order_usd:
            # Spec section 3: warn if an order would fall below the minimum.
            log.warning("%s %s notional %.2f below minimum %.2f; skipping.",
                        symbol, side, notional_usd, self.min_order_usd)
            raise RuntimeError("order below minimum notional")
        try:
            order = submit_market_order(self.trading, symbol, side, notional_usd)
            log.info("Order submitted: %s %s notional=%.2f id=%s",
                     symbol, side, notional_usd, order.id)
            try:
                fetched = self.trading.get_order_by_id(order.id)
                filled_qty = float(getattr(fetched, "filled_qty", 0.0) or 0.0)
                if filled_qty > 0:
                    return filled_qty
            except Exception:
                pass  # fill not settled yet; fall back to the estimate
            fill_price = getattr(order, "filled_avg_price", None)
            if fill_price:
                return qty_from_notional(notional_usd, float(fill_price),
                                         self.cfg["fee_pct_per_side"])
            # No fill info yet; estimate from this poll's price.
            return qty_from_notional(notional_usd, self.price,
                                     self.cfg["fee_pct_per_side"])
        except Exception as e:
            # Spec section 8: log; the loop waits one interval and retries.
            log.error("Order failed for %s %s: %s", symbol, side, e)
            raise


def check_minimums(cfg):
    """Spec 3: if the Alpaca minimum exceeds a slice size, stop and report."""
    problems = []
    for symbol in cfg["coins"]:
        for notional in (cfg["first_buy_usd"][symbol],
                         cfg["second_buy_usd"][symbol]):
            if notional < ALPACA_MIN_CRYPTO_ORDER_USD:
                problems.append(
                    f"{symbol}: order ${notional} below Alpaca minimum "
                    f"${ALPACA_MIN_CRYPTO_ORDER_USD}")
    if problems:
        for p in problems:
            log.error(p)
        log.error("Refusing to trade; increase slice sizes in config.json.")
        sys.exit(1)


def run_poll(cfg, state, trading, data_client, min_order_usd):
    """One poll: fetch all prices, step every coin, persist state."""
    prices = fetch_prices(data_client, cfg["coins"])
    for symbol in cfg["coins"]:
        coin = state.coins[symbol]
        price = prices[symbol]
        log.info("%s price=%.2f state=%s", symbol, price, coin.state)
        try:
            executor = LiveExecutor(trading, cfg, price, min_order_usd)
            step_coin(cfg, coin, price, executor)
        except Exception as e:
            # Spec 8: cap consecutive failed buys with a clear warning.
            if coin.state == "WAITING":
                coin.buy_retry_count += 1
                if coin.buy_retry_count >= 3:
                    log.warning("%s buy failed %d times in a row - please check "
                                "the account or config.", symbol,
                                coin.buy_retry_count)
            log.error("Step failed for %s: %s", symbol, e)
    save_state(state)


def handle_sigint(signum, frame):
    # Spec 8: state is saved after every action, so Ctrl+C just exits cleanly.
    log.info("Interrupt received; state already saved. Exiting.")
    sys.exit(0)


def main():
    setup_logging()
    signal.signal(signal.SIGINT, handle_sigint)
    cfg = load_config()
    check_minimums(cfg)
    trading, data_client = make_clients()
    state = load_state(cfg)
    min_order_usd = max(ALPACA_MIN_CRYPTO_ORDER_USD, cfg.get("min_order_usd", 0))
    log.info("Bot started. budget=$%.2f sell_target=%s%% fee=%s%% poll=%ss",
             cfg["budget_total_usd"], cfg["sell_target_pct"],
             cfg["fee_pct_per_side"], cfg["poll_interval_seconds"])
    while True:
        try:
            run_poll(cfg, state, trading, data_client, min_order_usd)
        except Exception as e:
            # Spec 8: fetch or order failure -> log, wait one interval, retry.
            log.error("Poll failed: %s", e)
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()

