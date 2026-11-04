"""Daily scanners keep independent durable identities and share account access."""
from pathlib import Path

from gate_runtime import CombinedHistory
from market import ONE_DAY_MS


class DailyReports:
    """Merge timeframes without changing price source or delivery history."""

    def __init__(self, primary, daily):
        self.primary, self.daily = primary, daily

    def is_admin(self, chat_id):
        return self.primary.is_admin(chat_id)

    def report_rows(self, chat_id):
        rows = self.primary.report_rows(chat_id)
        for source, store in self.daily:
            rows.extend(dict(row, timeframe='1D', exchange_name=store.exchange_name,
                             quote_key=(source, row['symbol']))
                        for row in store.report_rows(chat_id))
        return sorted(rows, key=lambda row: (row['signal_close_time'],
                      row['exchange_name'], row['symbol'], row.get('timeframe', '4h')))

    def get_ledger_started_at_ms(self):
        values = [self.primary.get_ledger_started_at_ms()]
        values.extend(store.get_ledger_started_at_ms() for _, store in self.daily)
        values = [value for value in values if type(value) is int]
        return min(values) if values else None


def build_daily(main, gate, state_path, universe, universe_version, history, reports,
                send_alert, stop, binance_client, gate_client, *, gate_enabled=True):
    from alerts import Scanner
    from approved_universe import configure_store
    from gate_market import GateClient
    from gate_runtime import GateStore
    from history import HistoryService
    from market import BinanceClient

    stores, scanners, services = [], [], []
    for source, original, market in [('binance', main, binance_client),
                                     ('gate', gate, gate_client)]:
        if original is None:
            continue
        path = (':memory:' if state_path == ':memory:' else
                str(Path(state_path).resolve().parent / (source + '-daily.sqlite3')))
        store = GateStore(path, main)
        store.timeframe = '1D'
        store.exchange_name = original.exchange_name
        symbols = universe['Binance' if source == 'binance' else 'Gate.io']
        configure_store(store, symbols, universe_version)
        original.repeat_sources = [original, store]
        store.repeat_sources = [original, store]
        stores.append((source, store))
        # Retain daily Gate reports even when its scanning flag is disabled.
        if source == 'gate' and not gate_enabled:
            continue
        client = (BinanceClient(interval_ms=ONE_DAY_MS) if source == 'binance' else
                  GateClient(binance_client, interval_ms=ONE_DAY_MS))
        # Reuse the platform transport, request lock and rate-limit cooldown.
        # Candle interval and discovery eligibility remain local to each scanner.
        client._get = market._get
        client.selected_symbols = symbols
        if source == 'gate':
            store.eligible_symbol = client.is_eligible

        def send(chat, symbol, close, text, source_store=store, source_client=client):
            if text.split('\n', 1)[0] != '📈 ' + symbol:
                raise ValueError('Daily signal heading and identity differ.')
            return send_alert(chat, text, signal_close_time=close,
                              source_store=source_store, source_client=source_client)

        scanners.append(Scanner(client, store, send_alert, send_signal=send))
        services.append(HistoryService(client, lambda chat, text: send_alert(chat, text, 'HTML'),
                                       stop, store=store, exchange_name=store.exchange_name))
    combined = CombinedHistory(history)
    combined.services.extend(services)
    return stores, scanners, combined, DailyReports(reports, stores)
