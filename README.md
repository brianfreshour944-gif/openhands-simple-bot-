# Simple Paper Trading Bot (Alpaca, PAPER money only)

A small rules-based crypto paper-trading bot. No AI, no indicators, no news —
it only reacts to fixed price levels, per the build spec. It trades **paper
money only** via the Alpaca PAPER endpoint and refuses to run in live mode.

## How it works

Each coin (BTC/USD, ETH/USD, SOL/USD) runs an independent state machine:

```
WAITING --price <= entry*(1+0.5%)--> HOLDING_1
HOLDING_1 --price >= avg_entry*(1+10%)--> HOLDING_2   (buy the second half)
HOLDING_1/HOLDING_2 --price >= avg_entry*(1+20%)--> DONE_CYCLE  (sell all)
DONE_CYCLE --> WAITING (new buys only when price is back at/below entry)
```

Deliberate non-rules (per spec): **no stop loss**, no shorting, no leverage,
no selling in pieces. If price falls, the bot holds and waits.

Fees: 0.25% per side (market orders are takers). Average entry = dollars spent
/ quantity received. A sell is only allowed at or above break-even (~0.5%
above average entry) as a safety check.

## Install

Python 3.11 or newer.

```bash
pip install -r requirements.txt
```

## Set credentials (environment variables)

Create a paper-trading account at https://alpaca.markets, then:

```bash
export ALPACA_API_KEY="your_paper_key"
export ALPACA_SECRET_KEY="your_paper_secret"
```

Keys are read from the environment only — never hard-code them. The bot
checks at startup that the trading client is on the PAPER endpoint and exits
if it is not.

## Run

```bash
python bot.py            # live paper-trading loop (polls every 60s)
```

Files created/updated automatically:

- `state.json` — per-coin state machine state, quantity, dollars spent,
  average entry, cycle count, cumulative profit, underwater days. Saved after
  every action; on restart the bot continues exactly where it left off (no
  duplicate buys).
- `trades.csv` — one row per order: timestamp, symbol, side, quantity, price,
  fee estimate, state before, state after, net profit (sells).
- `bot.log` — timestamped poll log: symbol, price, state, and every action.

## Offline simulation (no API calls)

```bash
python simulate.py                 # built-in demo price series
python simulate.py myseries.json   # {"BTC/USD": [...], "ETH/USD": [...]}
```

This replays a fake price series through the exact same rule logic
(`bot.step_coin`) the live bot uses, so you can verify the rules offline.

## Summary report

```bash
python summary.py
```

Prints total trades, cumulative profit, win count (profitable sells), and
days underwater per open position.

## Tests

```bash
pip install pytest
python -m pytest test_bot.py -v
```

Covers: first buy at entry (with tolerance), add-on at +10%, sell at +20%,
no sale at a loss, restart without duplicate buys, fee math (a full cycle
nets ~19.4%, ~$6.47 per $33.33 slice), trades.csv schema, underwater-day
counting, and error handling.

## Changing the config

Edit `config.json`; values are re-read fresh on every startup:

| key | default | meaning |
|---|---|---|
| `budget_total_usd` | 100 | notional budget the bot tracks for itself |
| `coins` | BTC/USD, ETH/USD, SOL/USD | traded symbols |
| `entry_price` | BTC 65000, ETH 1839, SOL 72 | per-coin entry levels |
| `entry_tolerance_pct` | 0.5 | buy when price ≤ entry × (1 + this/100) |
| `first_buy_usd` | 16.67/coin | first half of the slice |
| `add_trigger_pct` | 10 | second buy when price ≥ avg_entry × (1 + this/100) |
| `second_buy_usd` | 16.66/coin | second half of the slice |
| `sell_target_pct` | 20 | sell whole position at avg_entry × (1 + this/100) — may be 10–25 |
| `fee_pct_per_side` | 0.25 | taker fee per side |
| `poll_interval_seconds` | 60 | seconds between polls |
| `min_order_usd` | 1.0 | local minimum-notional guard |

The budget is the bot's own accounting: even if the paper account holds more
cash, the bot only ever deploys its own $100 (16.67 + 16.66 per coin).

Alpaca's crypto minimum order is $1 notional; at startup the bot checks every
configured order size against it and refuses to run if any slice is smaller.

## Ambiguity choices (simplest option, per spec note 11)

1. **Price source** — the latest bid price from Alpaca's crypto quote feed
   (`CryptoLatestQuoteRequest`). Bid is the conservative side for both
   triggering buys and valuing holdings.
2. **Sell order size** — sells are placed with the notional equal to the
   bot's computed proceeds, and the position quantity sold is the bot's
   tracked quantity (always the whole position).
3. **Fill quantity** — if Alpaca has not reported a fill yet at poll time, the
   bot estimates quantity from the fill/poll price with fees applied, so
   accounting is deterministic; the broker's reported fill quantity is used
   whenever it is available.
4. **Underwater days** — counted as calendar days (UTC) on which the latest
   price was below average entry at any poll during that day. Counted once
   per day, logged when it increments.
5. **Sell checked first** — in `HOLDING_1`, the sell target is checked before
   the add-on trigger on the same poll, so a coin that gaps straight to the
   target is sold rather than bought more.
6. **DONE_CYCLE** — transitions back to WAITING on the next poll; a new buy
   can only happen when price is again at or below the entry price, which is
   exactly the WAITING rule.
