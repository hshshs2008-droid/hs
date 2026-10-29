"""Gate source routing and status use isolated mocked workers, never real polling."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from alerts import AlertStore
from gate_runtime import GateStore
from indicators import Signal


class GateBotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.main = AlertStore(self.path / 'alerts.sqlite3')
        self.addCleanup(self.main.close)
        self.main.configure_access(100)
        self.main.subscribe(100, 1000)
        self.gate = GateStore(self.path / 'gate-alerts.sqlite3', self.main, activation_started_at_ms=0)
        self.addCleanup(self.gate.close)

    @patch('bot.api')
    def test_native_gate_pair_price_is_saved_only_in_gate_ledger(self, api):
        self.gate.subscribers()
        signal = Signal(2000, 19, 21, 10, 23, 14, 21, 2000, 2000)
        self.gate.record('OLD-V2_USDT', 2000, signal)
        text = self.gate.pending()[0][3]
        self.assertIn('🏷️ Gate.io Spot', text)
        self.assertNotIn('Binance Spot', text)
        client = Mock()
        client.latest_price.return_value = '0.0000123456'
        api.return_value = {'message_id': 1, 'date': 3, 'chat': {'id': 100}}
        bot.send_alert_message('test-token', client, 100, text, store=self.gate, signal_close_time=2000)
        client.latest_price.assert_called_once_with('OLD-V2_USDT')
        self.assertEqual(self.main.report_rows(100), [])
        self.assertEqual(self.gate.report_rows(100)[0]['quote_price'], '0.0000123456')
        self.assertEqual(self.gate.pending(), [])

    @patch('bot.api')
    def test_gate_discovery_scanner_and_confirmed_quote_work_together(self, api):
        from alerts import Scanner
        from gate_market import GateClient
        from market import FOUR_HOURS_MS
        boundary = (1792915200000 // FOUR_HOURS_MS) * FOUR_HOURS_MS
        now = boundary + 15000
        binance = Mock()
        binance.spot_symbols.return_value = ['BTCUSDT']
        client = GateClient(binance)
        def public_data(path, params=None):
            if path == '/spot/time': return {'server_time': now}
            if path == '/spot/currency_pairs':
                return [{'id': base + '_USDT', 'base': base, 'quote': 'USDT', 'trade_status': 'tradable'}
                        for base in ('BTC', 'ABC')]
            if path == '/spot/tickers':
                return [{'currency_pair': 'ABC_USDT', 'last': '2.125'}]
            if path == '/spot/candlesticks':
                return [[str((boundary - step * FOUR_HOURS_MS) // 1000), '10', '1.1', '1.2', '.9', '1', '9', 'true']
                        for step in (3, 2, 1)]
            raise AssertionError('Unexpected public request')
        client._get = public_data
        self.gate.eligible_symbol = client.is_eligible
        api.return_value = {'message_id': 7, 'date': now // 1000, 'chat': {'id': 100}}
        def send(chat, symbol, close, message):
            bot.send_alert_message('test-token', client, chat, message,
                                   store=self.gate, signal_close_time=close)
        scanner = Scanner(client, self.gate, Mock(), send_signal=send, request_delay=0,
                          clock=lambda: now / 1000, sleep=lambda seconds: None)
        signal = Signal(boundary - 1, 19, 21, 10, 23, 14, 21, boundary - 1, boundary - 1)
        with patch('alerts.entry_signal', side_effect=lambda candles: signal if candles[-1].close_time == signal.close_time else None):
            status = scanner.scan_once()
        self.assertEqual(status['state'], 'ready')
        self.assertEqual(status['symbols_total'], 1)
        self.assertEqual(status['alerts_sent'], 1)
        self.assertEqual(self.gate.report_rows(100)[0]['symbol'], 'ABC_USDT')
        self.assertEqual(self.gate.report_rows(100)[0]['quote_price'], '2.125')
        self.assertEqual(self.main.report_rows(100), [])
        self.assertEqual(self.gate.pending(), [])

    @patch('bot.api')
    def test_gate_status_is_separate_from_completed_binance_status(self, api):
        self.main.set_status({'state': 'ready', 'symbols_checked': 516, 'symbols_total': 516})
        self.gate.set_status({'state': 'blocked', 'symbols_checked': 25, 'symbols_total': 100})
        update = {'message': {'text': '/status', 'chat': {'id': 100, 'type': 'private'}, 'from': {'id': 100}}}
        bot.handle_update('test-token', update, self.main, gate_store=self.gate)
        text = api.call_args.args[2]['text']
        self.assertIn('آخر دورة مراقبة مكتملة', text)
        self.assertIn('516', text)
        self.assertIn('Gate.io Spot', text)
        self.assertIn('25/100', text)
        self.assertIn('تعذر إكمال', text)

    @patch('bot.api')
    def test_gate_status_and_source_details_are_not_exposed_to_unapproved_users(self, api):
        update = {'message': {'text': '/status', 'chat': {'id': 200, 'type': 'private'}, 'from': {'id': 200}}}
        bot.handle_update('test-token', update, self.main, gate_store=self.gate)
        replies = [call.args[2]['text'] for call in api.call_args_list if call.args[2].get('chat_id') == 200]
        self.assertNotIn('Gate.io Spot', replies[0])
        self.assertIn('طلب انضمامك', replies[0])

    @patch('bot.api')
    def test_start_explains_gate_only_policy_when_enabled(self, api):
        update = {'message': {'text': '/start', 'chat': {'id': 100, 'type': 'private'}, 'from': {'id': 100}}}
        bot.handle_update('test-token', update, self.main, gate_store=self.gate)
        self.assertIn('القائمة المعتمدة من CoinMarketCap', api.call_args.args[2]['text'])

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token', 'GATE_SPOT_ENABLED': 'invalid'}, clear=True)
    @patch('bot.api')
    def test_invalid_gate_configuration_does_not_start_any_worker(self, api):
        api.side_effect = [{}, {'url': ''}]
        with patch('bot.threading.Thread') as worker:
            with self.assertLogs('bot', level='ERROR'):
                self.assertEqual(bot.main(), 1)
        worker.assert_not_called()

    @patch("approved_universe.configure_store")
    def test_opt_in_main_wires_independent_gate_workers_and_qualified_reports(self, configure):
        main, gate = Mock(), Mock()
        main.admin_id = 100
        main.is_admin.return_value = True
        main.subscribers.return_value = [(100, 0)]
        main.get_update_offset.return_value = 0
        main.status.return_value = {'state': 'ready', 'symbols_checked': 516, 'symbols_total': 516}
        gate.status.return_value = {'state': 'scanning', 'symbols_checked': 25, 'symbols_total': 100}
        thread = Mock()
        thread.is_alive.return_value = False
        update = {'update_id': 20, 'message': {'text': '/status', 'chat': {'id': 100, 'type': 'private'}, 'from': {'id': 100}}}
        polls = iter([[update]])
        sent = []
        def fake_api(token, method, payload=None):
            if method == 'getWebhookInfo': return {'url': ''}
            if method == 'sendMessage': sent.append(payload['text'])
            if method == 'getUpdates':
                try: return next(polls)
                except StopIteration: raise KeyboardInterrupt
            return {}
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_ADMIN_ID': '100',
                                     'ALERT_STATE_PATH': str(self.path / 'mock.sqlite3'), 'GATE_SPOT_ENABLED': 'true'}, clear=True), \
                patch('bot.api', side_effect=fake_api), patch('alerts.AlertStore', return_value=main), \
                patch('gate_runtime.GateStore', return_value=gate) as gate_store, \
                patch('gate_market.GateClient') as gate_client, patch('market.BinanceClient') as binance_client, \
                patch('alerts.Scanner') as scanners, patch('history.HistoryService') as histories, \
                patch('reporting.ReportService') as reports, patch('bot.threading.Thread', return_value=thread):
            histories.return_value.is_alive.return_value = False
            histories.return_value.last_error = None
            histories.return_value.refresh_error = None
            reports.return_value.is_alive.return_value = False
            self.assertEqual(bot.main(), 0)
            self.assertEqual(scanners.call_count, 2)
            self.assertIs(scanners.call_args_list[0].args[0], binance_client.return_value)
            self.assertIs(scanners.call_args_list[1].args[0], gate_client.return_value)
            self.assertIn('send_signal', scanners.call_args_list[1].kwargs)
            self.assertEqual(histories.call_args_list[1].kwargs['exchange_name'], 'Gate.io Spot')
            self.assertEqual(thread.start.call_count, 2)
            self.assertIs(reports.call_args.args[0].main_store, main)
            self.assertIs(reports.call_args.args[0].gate_store, gate)
            self.assertIs(reports.call_args.args[1].binance_client, binance_client.return_value)
            gate_store.assert_called_once_with(str(self.path / 'gate-alerts.sqlite3'), main)
            main.close.assert_called_once()
            gate.close.assert_called_once()
        self.assertTrue(any('Gate.io Spot' in text and '25/100' in text for text in sent))


if __name__ == '__main__':
    unittest.main()
