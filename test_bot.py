"""Tests for the paper trading bot rules (spec section 10).

Run with: python -m pytest test_bot.py -v
"""

import json
import os
import sys
import tempfile

import pytest

import bot


@pytest.fixture
def cfg():
    # Config is read from the repo's own config.json (cwd-independent).
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    return bot.load_config(os.path.join(repo_dir, "config.json"))


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Redirect state/trades/log files so tests never touch real artifacts."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(bot, "TRADES_PATH", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(bot, "LOG_PATH", str(tmp_path / "bot.log"))
    yield


class FillRecorder:
    """Fake executor that records orders and simulates fee-aware fills."""

    def __init__(self, fee_pct, price=65000.0):
        self.fee_pct = fee_pct
        self.price = price
        self.orders = []

    def __call__(self, symbol, side, notional_usd):
        self.orders.append((symbol, side, notional_usd))
        if side == "buy":
            # Fill at the current poll price so avg_entry math mirrors live.
            return bot.qty_from_notional(notional_usd, self.price, self.fee_pct)
        return notional_usd / self.price


def fresh_coin(symbol="BTC/USD"):
    return bot.CoinState(symbol)


# ---------------------------------------------------------------------------
# Rule 1: first buy triggers at the entry price (with tolerance).
# ---------------------------------------------------------------------------

def test_first_buy_triggers_at_entry(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    # Price exactly at entry (BTC 65000) must trigger the first buy.
    bot.step_coin(cfg, coin, 65000.0, fills)
    assert coin.state == "HOLDING_1"
    assert len(fills.orders) == 1
    assert fills.orders[0][1] == "buy"
    assert pytest.approx(fills.orders[0][2], abs=0.01) == cfg["first_buy_usd"]["BTC/USD"]


def test_first_buy_triggers_within_tolerance(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"], price=65325.0)
    # 65000 * 1.005 = 65325 -> a price of 65325 is still a buy.
    bot.step_coin(cfg, coin, 65325.0, fills)
    assert coin.state == "HOLDING_1"


def test_no_buy_above_tolerance(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65326.0, fills)  # just above the tolerance band
    assert coin.state == "WAITING"
    assert fills.orders == []


# ---------------------------------------------------------------------------
# Rule 2: add-on buy triggers at +10% above average entry.
# ---------------------------------------------------------------------------

def test_addon_buy_triggers_at_plus_ten_pct(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)   # buy 1, avg = 65000*(1+fee)
    avg = coin.avg_entry
    fills.price = avg * 1.10
    bot.step_coin(cfg, coin, avg * 1.10, fills)  # exactly +10% -> buy 2
    assert coin.state == "HOLDING_2"
    assert len(fills.orders) == 2
    assert pytest.approx(fills.orders[1][2], abs=0.01) == cfg["second_buy_usd"]["BTC/USD"]


def test_no_addon_below_trigger(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)   # buy 1, avg ~65000
    bot.step_coin(cfg, coin, 70000.0, fills)   # ~+7.7% incl. fee, below +10%
    assert coin.state == "HOLDING_1"
    assert len(fills.orders) == 1


# ---------------------------------------------------------------------------
# Rule 3: sell triggers at the target (+20% above average entry).
# ---------------------------------------------------------------------------

def test_sell_triggers_at_target(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)   # buy 1, avg ~65000
    target = bot.sell_trigger_price(cfg, coin)
    fills.price = target
    bot.step_coin(cfg, coin, target, fills)    # +20% -> sell (from HOLDING_1)
    assert coin.state == "DONE_CYCLE"
    sells = [o for o in fills.orders if o[1] == "sell"]
    assert len(sells) == 1


def test_sell_after_both_buys(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)   # buy 1
    bot.step_coin(cfg, coin, 71500.0, fills)   # buy 2 (avg ~65162)
    bot.step_coin(cfg, coin, 73000.0, fills)   # modest rise, no sell yet
    assert coin.state == "HOLDING_2"
    target = bot.sell_trigger_price(cfg, coin)
    fills.price = target * 1.01
    bot.step_coin(cfg, coin, target * 1.01, fills)  # above avg * 1.2 -> sell
    assert coin.state == "DONE_CYCLE"


def test_cycle_resets_to_waiting(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)
    target = bot.sell_trigger_price(cfg, coin)
    fills.price = target * 1.01
    bot.step_coin(cfg, coin, target * 1.01, fills)   # sell
    assert coin.state == "DONE_CYCLE"
    # Rule 4: a new buy may only happen when price is back at/below entry.
    bot.step_coin(cfg, coin, 75000.0, fills)   # too high -> still WAITING, no buy
    assert coin.state == "WAITING"
    assert len(fills.orders) == 2  # buy1 + sell only
    fills.price = 65000.0
    bot.step_coin(cfg, coin, 65000.0, fills)   # back at entry -> buy again
    assert coin.state == "HOLDING_1"
    assert coin.cycle_count == 1


# ---------------------------------------------------------------------------
# Rule 5 / 6: no sale ever happens at a loss.
# ---------------------------------------------------------------------------

def test_no_sale_at_loss(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)   # buy 1 at avg=65000
    # Crash far below entry: HOLDING_1 must hold, never sell at a loss.
    for p in (60000, 50000, 40000, 30000):
        bot.step_coin(cfg, coin, p, fills)
    assert coin.state == "HOLDING_1"
    assert not any(o[1] == "sell" for o in fills.orders)
    assert coin.qty > 0


# ---------------------------------------------------------------------------
# Section 6: fee math and the +20% -> ~19.4% / ~$6.47 expectation.
# ---------------------------------------------------------------------------

def test_twenty_pct_gain_nets_about_19_4_pct_and_6_47_dollars(cfg):
    """Buying $33.33 at entry and selling at +20% nets ~19.4% after fees."""
    fee = cfg["fee_pct_per_side"]
    spend = cfg["budget_total_usd"] / 3           # $33.33 per coin slice
    price = cfg["entry_price"]["BTC/USD"]         # 65000
    qty = bot.qty_from_notional(spend, price, fee)
    sell_price = price * (1 + cfg["sell_target_pct"] / 100)
    proceeds = bot.proceeds_from_qty(qty, sell_price, fee)
    profit = proceeds - spend
    pct = profit / spend * 100
    # Spec: about 19.4% and about $6.47 per coin slice.
    assert pct == pytest.approx(19.4, abs=0.1)
    assert profit == pytest.approx(6.47, abs=0.02)
    # Sanity: 33.33 * 1.194 ~ 39.80.
    assert proceeds == pytest.approx(spend * 1.194, abs=0.02)


def test_avg_entry_is_dollars_over_quantity(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)
    assert coin.avg_entry == pytest.approx(coin.dollars_spent / coin.qty)
    # dollars_spent includes the fee already accounted in the qty calc.
    assert coin.avg_entry == pytest.approx(
        65000 * (1 + cfg["fee_pct_per_side"] / 100), rel=1e-9)


# ---------------------------------------------------------------------------
# Section 7: restart must not cause a duplicate buy.
# ---------------------------------------------------------------------------

def test_restart_does_not_duplicate_buy(cfg, tmp_path):
    state_path = str(tmp_path / "state.json")
    fills = FillRecorder(cfg["fee_pct_per_side"])

    # First run: buy 1, then persist state.
    coin = fresh_coin()
    bot.step_coin(cfg, coin, 65000.0, fills)   # buy 1
    bot.save_state(bot.BotState({"BTC/USD": coin}), state_path)

    # Restart: reload state from disk into a brand-new CoinState.
    cfg2 = cfg
    reloaded = bot.load_state(cfg2, state_path)
    coin2 = reloaded.coins["BTC/USD"]
    assert coin2.state == "HOLDING_1"
    assert coin2.qty > 0

    # Price is still within the entry band; a buggy bot would buy again.
    fills2 = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg2, coin2, 65000.0, fills2)
    assert coin2.state == "HOLDING_1"
    assert fills2.orders == []          # no duplicate buy
    assert coin2.qty == coin.qty        # quantity unchanged
    assert coin2.dollars_spent == coin.dollars_spent


def test_state_roundtrip_all_fields(cfg, tmp_path):
    state_path = str(tmp_path / "state.json")
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)
    coin.underwater_days = 3.0
    bot.save_state(bot.BotState({"BTC/USD": coin}), state_path)
    reloaded = bot.load_state(cfg, state_path).coins["BTC/USD"]
    assert reloaded.state == "HOLDING_1"
    assert reloaded.underwater_days == 3.0
    assert reloaded.dollars_spent == coin.dollars_spent


# ---------------------------------------------------------------------------
# Section 7: trades.csv columns and underwater-day tracking.
# ---------------------------------------------------------------------------

def test_trades_csv_columns(cfg):
    import csv as csv_mod
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills, today="2026-01-01")
    target = bot.sell_trigger_price(cfg, coin)
    fills.price = target
    bot.step_coin(cfg, coin, target, fills, today="2026-01-02")   # sell
    with open(bot.TRADES_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv_mod.DictReader(f))
    assert rows[0].keys() >= set(bot.TRADE_FIELDS)
    assert rows[0]["side"] == "buy"
    assert rows[-1]["side"] == "sell"
    assert float(rows[-1]["net_profit"]) > 0


def test_underwater_days_counted_once_per_day(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills, today="2026-01-01")
    # Three separate polls on one calendar day, then one on the next day.
    for _ in range(3):
        bot.step_coin(cfg, coin, 60000.0, fills, today="2026-01-02")
    bot.step_coin(cfg, coin, 60000.0, fills, today="2026-01-03")
    assert coin.underwater_days == 2.0  # one day + one next day, not 5 polls


# ---------------------------------------------------------------------------
# Section 8: order failures never crash the loop; sells retry next poll.
# ---------------------------------------------------------------------------

def test_failed_order_does_not_change_state(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)

    class ExplodingExecutor:
        def __call__(self, *a, **k):
            raise RuntimeError("API down")

    target = bot.sell_trigger_price(cfg, coin)
    bot.step_coin(cfg, coin, target, ExplodingExecutor())  # sell attempt fails
    assert coin.state == "HOLDING_1"     # state unchanged
    assert coin.qty > 0                  # still holding
    # Next poll with a working executor still sells at the target.
    fills.price = target
    bot.step_coin(cfg, coin, target, fills)
    assert coin.state == "DONE_CYCLE"


def test_buy_retry_counter_resets_after_success(cfg):
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    coin.buy_retry_count = 2
    bot.step_coin(cfg, coin, 65000.0, fills)
    assert coin.buy_retry_count == 0


# ---------------------------------------------------------------------------
# Rule 4: DONE_CYCLE always resets to WAITING on the next poll - the bot can
# never get stuck there and stop trading a coin.
# ---------------------------------------------------------------------------

def test_done_cycle_resets_on_next_poll_at_any_price(cfg):
    # Drive a fresh coin to DONE_CYCLE, then step one poll at each price.
    # At every price the reset must happen: the bot can never get stuck.
    for price in (100000.0, 20000.0, 65000.0):
        coin = fresh_coin()
        fills = FillRecorder(cfg["fee_pct_per_side"], price=65000.0)
        bot.step_coin(cfg, coin, 65000.0, fills)          # buy 1 -> HOLDING_1
        target = bot.sell_trigger_price(cfg, coin)
        fills.price = target * 1.01
        bot.step_coin(cfg, coin, target * 1.01, fills)    # sell -> DONE_CYCLE
        assert coin.state == "DONE_CYCLE"

        # Reset poll: DONE_CYCLE -> WAITING, and no buy on this same poll.
        fills.price = price
        bot.step_coin(cfg, coin, price, fills)
        assert coin.state == "WAITING", f"stuck after sell at price {price}"
        assert len(fills.orders) == 2  # buy1 + sell only, no extra buy

        # From WAITING, a new buy only happens when price is at/below entry.
        if price <= 65000.0 * 1.005:
            bot.step_coin(cfg, coin, price, fills)
            assert coin.state == "HOLDING_1"
        else:
            bot.step_coin(cfg, coin, price, fills)
            assert coin.state == "WAITING"  # too high - no buy


def test_done_cycle_survives_restart_and_resets(cfg, tmp_path):
    """Bot killed right after a sell (state saved as DONE_CYCLE) must still
    resume trading that coin after a restart - never stuck."""
    state_path = str(tmp_path / "state.json")
    coin = fresh_coin()
    fills = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, coin, 65000.0, fills)
    target = bot.sell_trigger_price(cfg, coin)
    fills.price = target * 1.01
    bot.step_coin(cfg, coin, target * 1.01, fills)   # sell -> DONE_CYCLE
    bot.save_state(bot.BotState({"BTC/USD": coin}), state_path)

    # Restart from disk; next poll resets to WAITING and the cycle can repeat.
    reloaded = bot.load_state(cfg, state_path).coins["BTC/USD"]
    assert reloaded.state == "DONE_CYCLE"
    fills2 = FillRecorder(cfg["fee_pct_per_side"])
    bot.step_coin(cfg, reloaded, 90000.0, fills2)    # high price; just resets
    assert reloaded.state == "WAITING"
    assert fills2.orders == []                       # no buy at a high price
    fills2.price = 65000.0
    bot.step_coin(cfg, reloaded, 65000.0, fills2)    # back at entry -> new cycle
    assert reloaded.state == "HOLDING_1"
    assert reloaded.cycle_count == 1                 # prior cycle was recorded
