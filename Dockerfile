FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ALERT_STATE_PATH=/data/alerts.sqlite3 \
    API_BINANCE_BASE=https://data-api.binance.vision

WORKDIR /app
COPY alerts.py bot.py history.py indicators.py market.py run_bot.py reporting.py gate_market.py gate_runtime.py ./

# Verify the base image supplies Linux locking, TLS roots and Israel time data.
RUN mkdir -p /data && python3 -B -c "import fcntl, ssl; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Jerusalem'); assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0; import bot, alerts, history, indicators, market, run_bot, reporting, gate_market, gate_runtime"

CMD ["python3", "-B", "-u", "/app/run_bot.py"]
