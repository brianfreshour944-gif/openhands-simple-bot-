"""Print a summary of the paper-trading bot's activity.

Reads trades.csv and state.json and prints: total trades, cumulative profit,
win count (profitable sells), and days underwater per open position.

Usage: python summary.py
"""

import csv
import json
import os

TRADES_PATH = "trades.csv"
STATE_PATH = "state.json"


def main():
    trades_path = os.environ.get("BOT_TRADES_PATH", TRADES_PATH)
    state_path = os.environ.get("BOT_STATE_PATH", STATE_PATH)

    rows = []
    if os.path.exists(trades_path):
        with open(trades_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    buys = sum(1 for r in rows if r["side"] == "buy")
    sells = [r for r in rows if r["side"] == "sell"]
    wins = sum(1 for r in sells if float(r["net_profit"] or 0) > 0)
    losses = sum(1 for r in sells if float(r["net_profit"] or 0) < 0)
    total_profit = sum(float(r["net_profit"] or 0) for r in sells)

    print("=== Paper Trading Bot Summary ===")
    print(f"Total trades: {len(rows)} ({buys} buys, {len(sells)} sells)")
    print(f"Win count:    {wins}  (losing sells: {losses})")
    print(f"Cumulative profit: ${total_profit:.4f}")

    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        print("\nOpen positions:")
        any_open = False
        for symbol, c in state.get("coins", {}).items():
            if c.get("qty", 0) > 0 or c.get("underwater_days", 0) > 0:
                any_open = True
                print(f"  {symbol}: state={c.get('state')} "
                      f"qty={c.get('qty'):.10f} "
                      f"avg_entry={c.get('avg_entry'):.2f} "
                      f"days_underwater={c.get('underwater_days', 0):.0f} "
                      f"cycles={c.get('cycle_count')} "
                      f"profit=${c.get('cumulative_profit', 0):.4f}")
        if not any_open:
            print("  (none)")
    else:
        print("\nNo state.json yet - bot has not run.")


if __name__ == "__main__":
    main()
