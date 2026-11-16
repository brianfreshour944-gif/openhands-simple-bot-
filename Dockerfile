# Paper trading bot - PAPER money only.
# Runs bot.py from /app so config.json, state.json, trades.csv and bot.log
# stay in the container's working directory (mount a volume for /app to
# persist state across restarts).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.json ./

# Refuses to start unless ALPACA_API_KEY / ALPACA_SECRET_KEY are set
# and the client is in paper mode.
CMD ["python", "bot.py"]
