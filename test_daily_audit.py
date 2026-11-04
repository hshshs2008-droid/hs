"""Exercise daily production wiring with real parsers/SQLite and fake transports."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alerts import AlertStore
from bot import send_alert_message
from daily_runtime import build_daily
from gate_market import GateClient
from gate_runtime import GateStore, ReportStore
from indicators import entry_signal
from market import BinanceClient, Candle, ONE_DAY_MS as DAY

MIDNIGHT = 1793836800000


class DailyAuditTests(unittest.TestCase):
    def test_daily_strategy_same_candle_adjacent_both_orders_and_threshold_priority(self):
        candles = [Candle(MIDNIGHT + i*DAY, MIDNIGHT+(i+1)*DAY-1, 10, 11, 9, 10)
                   for i in range(3)]
        for level in (20, 30):
            cases = [([level-2, level-1, level+3], [8,9,15], [10,11,13], (2,2)),
                     ([level-1, level+2, level+3], [8,9,15], [10,11,13], (1,2)),
                     ([level-2, level-1, level+3], [8,15,26], [10,13,18], (2,1))]
            for r, k, d, expected in cases:
                with self.subTest(level=level, events=expected), \
                     patch('indicators.rsi', return_value=r), \
                     patch('indicators.stochastic', return_value=(k,d)):
                    signal = entry_signal(candles)
                    self.assertIsNotNone(signal)
                    self.assertEqual(signal.rsi_threshold, level)
                    self.assertEqual(signal.rsi_event_close_time, candles[expected[0]].close_time)
                    self.assertEqual(signal.stoch_event_close_time, candles[expected[1]].close_time)
        with patch('indicators.rsi', return_value=[18,19,35]), \
             patch('indicators.stochastic', return_value=([8,9,15],[10,11,13])):
            self.assertEqual(entry_signal(candles).rsi_threshold, 30)

    def test_both_exchanges_send_daily_then_restart_without_replaying(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'alerts.sqlite3'
            main = AlertStore(path)
            self.addCleanup(main.close)
            main.configure_access(100)
            main.subscribe(100, 1)
            gate = GateStore(Path(directory)/'gate-alerts.sqlite3', main,
                             activation_started_at_ms=1)
            self.addCleanup(gate.close)
            binance_client = BinanceClient()
            gate_client = GateClient(binance_client)
            now = MIDNIGHT+15000
            calls = []

            def get(source, endpoint, params=None):
                calls.append((source, endpoint, params))
                if endpoint == '/api/v3/time': return {'serverTime': now}
                if endpoint == '/spot/time': return {'server_time': now}
                if endpoint == '/api/v3/ticker/price':
                    return {'symbol': 'BTCUSDT', 'price': '10'}
                if endpoint == '/spot/tickers':
                    return [{'currency_pair': 'ABC_USDT', 'last': '20'}]
                if endpoint in ('/api/v3/klines', '/spot/candlesticks'):
                    self.assertEqual(params['interval'], '1d')
                    rows = []
                    for i in (-3,-2,-1,0):
                        opened = MIDNIGHT+i*DAY
                        rows.append([opened, '10','11','9','10','1',opened+DAY-1]
                                    if source == 'binance' else
                                    [str(opened//1000),'1','10','11','9','10','1','true'])
                    return rows
                raise AssertionError(endpoint)

            binance_client._get = lambda endpoint, params=None: get('binance', endpoint, params)
            gate_client._get = lambda endpoint, params=None: get('gate', endpoint, params)
            universe = {'Binance': frozenset({'BTCUSDT'}), 'Gate.io': frozenset({'ABC_USDT'})}
            delivered = []

            def telegram(token, method, payload):
                self.assertEqual(method, 'sendMessage')
                delivered.append(payload)
                return {'message_id': len(delivered), 'date': now//1000, 'chat': {'id': 100}}

            def send(chat, text, parse_mode=None, **kwargs):
                return send_alert_message('test-token', kwargs['source_client'], chat, text,
                                          store=kwargs['source_store'],
                                          signal_close_time=kwargs['signal_close_time'])

            def build():
                stores, scanners, history, reports = build_daily(
                    main, gate, str(path), universe, 'daily-audit-v1', Mock(),
                    ReportStore(main, gate), send, threading.Event(), binance_client, gate_client)
                for source, store in stores:
                    if source == 'gate': store.eligible_symbol = lambda symbol: symbol == 'ABC_USDT'
                for scanner, symbol in zip(scanners, ('BTCUSDT', 'ABC_USDT')):
                    scanner.client.spot_symbols = Mock(return_value=[symbol])
                    scanner.clock = lambda: now/1000
                    scanner.sleep = lambda _: None
                    scanner.request_delay = 0
                return stores, scanners, reports

            with patch('time.time', return_value=(MIDNIGHT-DAY//2)/1000):
                stores, scanners, reports = build()
            try:
                with patch('indicators.rsi', return_value=[18,19,35]), \
                     patch('indicators.stochastic', return_value=([8,9,15],[10,11,13])), \
                     patch('bot.api', side_effect=telegram), patch('time.time', return_value=now/1000):
                    for scanner in scanners:
                        result = scanner.scan_once()
                        self.assertEqual(result['state'], 'ready')
                        self.assertEqual(result['alerts_sent'], 1)
                self.assertEqual(len(delivered), 2)
                for payload in delivered:
                    self.assertIn('⏳ 1D', payload['text'])
                    self.assertIn('interval=D', payload['entities'][0]['url'])
                self.assertEqual([r['quote_price'] for r in reports.report_rows(100)], ['10','20'])
                self.assertEqual({r['timeframe'] for r in reports.report_rows(100)}, {'1D'})
                activations = [store.activation_started_at_ms for _,store in stores]
                floors = [store.strategy_activation_ms for _,store in stores]
            finally:
                for _,store in stores: store.close()
            calls.clear()
            with patch('time.time', return_value=(MIDNIGHT+2*3600000)/1000):
                stores, scanners, reports = build()
            try:
                self.assertEqual([store.activation_started_at_ms for _,store in stores], activations)
                self.assertEqual([store.strategy_activation_ms for _,store in stores], floors)
                with patch('bot.api') as api:
                    for scanner in scanners:
                        self.assertEqual(scanner.scan_once()['alerts_sent'], 0)
                    api.assert_not_called()
                self.assertFalse(any(endpoint in ('/api/v3/klines','/spot/candlesticks')
                                     for _,endpoint,_ in calls))
                self.assertEqual(len(reports.report_rows(100)), 2)
            finally:
                for _,store in stores: store.close()

    def test_activation_ignores_old_daily_close_and_preserves_pending_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            main = AlertStore(Path(directory)/'main.sqlite3')
            self.addCleanup(main.close)
            main.configure_access(100)
            main.subscribe(100, 1)
            from indicators import Signal
            path = Path(directory)/'daily.sqlite3'
            daily = GateStore(path, main, activation_started_at_ms=MIDNIGHT+1000)
            daily.timeframe = '1D'
            def signal(close): return Signal(close, 29, 31, 9, 15, 11, 13, close, close)
            daily.record('BTCUSDT', MIDNIGHT-1, signal(MIDNIGHT-1))
            self.assertEqual(daily.pending(), [])
            daily.record('BTCUSDT', MIDNIGHT+DAY-1, signal(MIDNIGHT+DAY-1))
            self.assertEqual(len(daily.pending()), 1)
            daily.close()
            daily = GateStore(path, main, activation_started_at_ms=MIDNIGHT+2*DAY)
            try:
                self.assertEqual(len(daily.pending()), 1)
                self.assertIn('⏳ 1D', daily.pending()[0][3])
                main.unsubscribe(100)
                self.assertEqual(daily.pending(), [])
            finally:
                daily.close()


if __name__ == '__main__':
    unittest.main()
