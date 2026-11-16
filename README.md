# openhands-simple-bot

Places GTC limit buy orders at the current market price for BTC, ETH, and SOL.

## Safety model (fail-closed)

- `DRY_RUN` defaults to `1`: orders are printed, never sent.
- The Alpaca base URL is forced to the paper endpoint
  (`https://paper-api.alpaca.markets`) unless `ALLOW_LIVE=1` is set.
- Orders are only sent when Alpaca API keys are present.

## Environment variables

| Variable         | Default                          | Meaning                                   |
|------------------|----------------------------------|-------------------------------------------|
| `DRY_RUN`        | `1`                              | `1` = print orders only, `0` = send them  |
| `ALLOW_LIVE`     | `0`                              | Required to point at a non-paper endpoint |
| `NOTIONAL_USD`   | `100`                            | Dollar amount to buy per symbol           |
| `ALPACA_API_KEY` | —                                | Alpaca key ID (required to send orders)   |
| `ALPACA_SECRET_KEY` | —                             | Alpaca secret key (required to send)      |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | Order endpoint                        |

Prices come from Coinbase's public spot API; no key needed for that.

## Run

```bash
pip install -r requirements.txt
python bot.py
```

## Tests

```bash
pip install pytest
pytest tests/
```
