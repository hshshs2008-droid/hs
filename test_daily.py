"""Daily boundaries, independent delivery identities and cross-frame reporting."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alerts import AlertStore, Scanner, format_alert, _refresh_alert_times
from bot import send_alert_message
from daily_runtime import build_daily, DailyReports
from gate_market import GateClient
from gate_runtime import GateStore, ReportStore, MarketRouter
from history import HistoryService, HistoryResult, format_history
from indicators import Signal
from market import BinanceClient, ONE_DAY_MS as DAY, FOUR_HOURS_MS, MarketError, Candle
from reporting import build_report
from test_reporting import cells

BOUNDARY = 1793836800000  # UTC midnight, 2026-11-05


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.main = AlertStore(Path(self.directory.name) / 'alerts.sqlite3')
        self.addCleanup(self.main.close)
        self.main.configure_access(100)
        self.main.subscribe(100, 1)
        self.daily = GateStore(Path(self.directory.name) / 'daily.sqlite3', self.main,
                               activation_started_at_ms=1)
        self.addCleanup(self.daily.close)
        self.daily.timeframe = '1D'
        self.daily.exchange_name = 'Binance Spot'
        self.main.repeat_sources = self.daily.repeat_sources = [self.main, self.daily]

    def signal(self, close=BOUNDARY-1, threshold=30):
        return Signal(close, 19, 35, 10, 23, 14, 21, close, close,
                      rsi_threshold=threshold)

    def receipt(self, store, close, sent):
        store.record('BTCUSDT', close, self.signal(close))
        attempt = store.prepare_delivery(100, 'BTCUSDT', close, '10', sent, 'message', sent)
        store.confirm_delivery(attempt, 1, sent, sent)

    def test_same_coin_same_close_survives_both_frames_and_combines_weekly_repeat(self):
        close = BOUNDARY-1
        self.receipt(self.main, close, BOUNDARY)
        self.assertEqual(len(self.main.report_rows(100)), 1)
        self.assertIn('(2/7D)', self.daily.repeat_alert_note(100, 'BTCUSDT', close, BOUNDARY))
        self.receipt(self.daily, close, BOUNDARY+1000)
        reports = DailyReports(ReportStore(self.main), [('binance', self.daily)])
        rows = reports.report_rows(100)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.get('timeframe', '4h') for r in rows], ['1D', '4h'])
        self.assertIn('(3/7D)', self.main.repeat_alert_note(100, 'BTCUSDT', close+DAY, BOUNDARY+DAY))
        self.assertEqual(self.main.repeat_alert_note(101, 'BTCUSDT', close+DAY, BOUNDARY+DAY), '')
        self.assertEqual(self.main.repeat_alert_note(100, 'BTCUSDT', close+9*DAY, BOUNDARY+9*DAY), '')
        workbook = cells(build_report(rows, {('binance', 'BTCUSDT'): {'price': '12', 'at_ms': BOUNDARY}}, BOUNDARY))
        self.assertEqual(workbook[0][5], 'الفريم')
        self.assertEqual({r[5] for r in workbook[1:]}, {'4h', '1D'})

    def test_daily_format_preserves_threshold_and_queued_message_frame(self):
        text = format_alert('BTCUSDT', self.signal(threshold=20), timeframe='1D')
        self.assertIn('⏳ 1D • شموع مغلقة', text)
        self.assertIn('🕒 1D إغلاق الشمعة:', text)
        self.assertIn('💎', text)
        self.assertNotIn('4h', text)
        self.assertEqual(_refresh_alert_times(text, BOUNDARY-1, BOUNDARY-1, BOUNDARY-1), text)
        with patch('bot.api', return_value={'message_id': 1, 'date': BOUNDARY//1000}) as api:
            client = Mock()
            client.latest_price.return_value = '10'
            send_alert_message('test-token', client, 100, text)
        self.assertIn('interval=D', api.call_args.args[2]['entities'][0]['url'])

    def test_clients_request_closed_daily_candles_and_reject_misalignment(self):
        for client in (BinanceClient(interval_ms=DAY), GateClient(BinanceClient(), interval_ms=DAY)):
            with self.subTest(client=type(client).__name__):
                gate = isinstance(client, GateClient)
                def row(opened):
                    return ([str(opened//1000), '10', '1.1', '1.2', '.9', '1', '9', 'true'] if gate else
                            [opened, '1', '1.2', '.9', '1.1', '10', opened+DAY-1])
                client._get = Mock(return_value=[row(BOUNDARY-DAY), row(BOUNDARY)])
                result = client.candles('BTC_USDT' if gate else 'BTCUSDT', BOUNDARY+15000)
                self.assertEqual(client._get.call_args.args[1]['interval'], '1d')
                self.assertEqual([r.close_time for r in result], [BOUNDARY-1])
                client._get.return_value = [row(BOUNDARY-DAY+FOUR_HOURS_MS)]
                with self.assertRaises(MarketError):
                    client.candles('BTC_USDT' if gate else 'BTCUSDT', BOUNDARY+15000)

    def test_scanner_waits_for_daily_grace_then_checkpoints_whole_day(self):
        client = Mock(interval_ms=DAY)
        client.spot_symbols.return_value = ['BTCUSDT']
        client.server_time.return_value = BOUNDARY+15000
        client.candles.return_value = [Candle(BOUNDARY-DAY, BOUNDARY-1, 1, 2, .5, 1)]
        scanner = Scanner(client, self.daily, Mock(), request_delay=0,
                          clock=lambda: client.server_time.return_value/1000, sleep=lambda _: None)
        with patch('alerts.entry_signal', return_value=None):
            self.assertEqual(scanner.scan_once()['target_close_time'], BOUNDARY-1)
            client.server_time.return_value = BOUNDARY+DAY+5000
            self.assertEqual(scanner.scan_once()['target_close_time'], BOUNDARY-1)
            self.assertEqual(client.candles.call_count, 1)
            client.server_time.return_value = BOUNDARY+DAY+15000
            client.candles.return_value = [Candle(BOUNDARY, BOUNDARY+DAY-1, 1, 2, .5, 1)]
            self.assertEqual(scanner.scan_once()['target_close_time'], BOUNDARY+DAY-1)
            self.assertEqual(client.candles.call_count, 2)

    def test_daily_history_frame_persists_across_restart(self):
        client = Mock(interval_ms=DAY)
        service = HistoryService(client, Mock(), threading.Event(), store=self.daily,
                                 clock=lambda: (BOUNDARY+15000)/1000)
        result = HistoryResult(BOUNDARY+15000, BOUNDARY-1, 1, 1,
                               (('BTCUSDT', self.signal()),), True, {}, timeframe='1D')
        service._cache = result
        service._persist_cache()
        restored = HistoryService(client, Mock(), threading.Event(), store=self.daily,
                                  clock=lambda: (BOUNDARY+15000)/1000)
        self.assertEqual(restored.cached_result().timeframe, '1D')
        self.assertIn('<code>1D</code>', format_history(restored.cached_result())[0])

    def test_runtime_uses_approved_symbols_shared_transport_and_binance_only_router(self):
        market = BinanceClient()
        history = Mock()
        universe = {'Binance': frozenset({'BTCUSDT'}), 'Gate.io': frozenset()}
        stores, scanners, combined, reports = build_daily(
            self.main, None, ':memory:', universe, 'test-version', history,
            ReportStore(self.main), Mock(), threading.Event(), market, None, gate_enabled=False)
        self.addCleanup(stores[0][1].close)
        self.assertEqual(len(scanners), 1)
        self.assertEqual(scanners[0].client.interval_ms, DAY)
        self.assertEqual(scanners[0].client.selected_symbols, universe['Binance'])
        self.assertEqual(scanners[0].client._get, market._get)
        self.assertEqual(len(combined.services), 2)
        with patch.object(market, 'latest_price', return_value='12'):
            self.assertEqual(MarketRouter(market).latest_price(('binance', 'BTCUSDT')), '12')


if __name__ == '__main__':
    unittest.main()
