"""Exercise exact live quotes, private report routing, and file upload without real services."""
import io
import json
import os
import tempfile
import unittest
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import bot
from alerts import AlertStore


class ReportBotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = AlertStore(Path(self.folder.name) / 'alerts.sqlite3')
        self.addCleanup(self.store.close)
        self.store.configure_access(100)
        self.client = Mock()
        self.client.latest_price.return_value = '0.0000123456789012345678'
        self.text = '📈 BTCUSDT\n🕒 إغلاق شمعة 4h: 2026-10-25 (10:00)'

    def receipt(self, chat_id=100, message_id=20, date=1792915201):
        return {'message_id': message_id, 'date': date, 'chat': {'id': chat_id}}

    def deliver(self, chat_id=100, **kwargs):
        return bot.send_alert_message('test-token', self.client, chat_id, self.text,
                                      store=self.store, signal_close_time=1792915199999, **kwargs)

    def command(self, command='/report', user=100, kind='private', sender=None, reports=None):
        bot.handle_update('test-token', {'message': {'text': command,
                          'chat': {'id': user, 'type': kind},
                          'from': {'id': user if sender is None else sender}}},
                          self.store, reports=reports)

    @patch('bot.api')
    def test_exact_payload_and_quote_committed_before_api_and_receipt_confirmed_after(self, api):
        def acknowledge(token, method, payload):
            attempt = self.store._db.execute(
                'SELECT quote_price,message_text,status FROM delivery_ledger').fetchone()
            self.assertEqual(attempt, (self.client.latest_price.return_value, payload['text'], 'prepared'))
            self.assertEqual(self.store.report_rows(100), [])
            return self.receipt()
        api.side_effect = acknowledge
        self.deliver()
        row, = self.store.report_rows(100)
        self.assertEqual(row['quote_price'], '0.0000123456789012345678')
        self.assertIn('💵 0.0000123456789012345678 USDT', row['message_text'])
        self.assertEqual(row['telegram_message_id'], 20)
        self.assertEqual(row['telegram_sent_at_ms'], 1792915201000)

    @patch('bot.api')
    def test_failed_attempt_is_not_exported_and_retry_uses_actual_new_price(self, api):
        self.client.latest_price.side_effect = ['1.0', '1.5']
        api.side_effect = [bot.TelegramError('rate limited', status=429), self.receipt()]
        with self.assertRaises(bot.TelegramError):
            self.deliver()
        self.assertEqual(self.store.report_rows(100), [])
        self.deliver()
        self.assertEqual(self.store.report_rows(100)[0]['quote_price'], '1.5')
        self.assertEqual([r[0] for r in self.store._db.execute(
            'SELECT status FROM delivery_ledger ORDER BY rowid')], ['failed', 'sent'])

    @patch('bot.api')
    def test_transport_failure_is_uncertain_and_does_not_claim_success(self, api):
        api.side_effect = bot.TelegramError('network unavailable')
        with self.assertRaises(bot.TelegramError):
            self.deliver()
        self.assertEqual(self.store.report_rows(100), [])
        self.assertEqual(self.store._db.execute('SELECT status FROM delivery_ledger').fetchone()[0], 'uncertain')

    @patch('bot.api')
    def test_unexpected_error_status_is_sanitized_and_keeps_uncertainty(self, api):
        api.side_effect = bot.TelegramError('malformed', status='untrusted')
        with self.assertRaises(bot.TelegramError):
            self.deliver()
        self.assertEqual(self.store._db.execute('SELECT status FROM delivery_ledger').fetchone()[0], 'uncertain')

    @patch('bot.api')
    def test_missing_market_price_is_recorded_as_unknown_without_fabrication(self, api):
        self.client.latest_price.side_effect = bot.MarketError('unavailable', status=429)
        api.return_value = self.receipt()
        with self.assertLogs('bot', level='WARNING'):
            self.deliver()
        row, = self.store.report_rows(100)
        self.assertIsNone(row['quote_price'])
        self.assertIsNone(row['quote_at_ms'])
        self.assertIn('السعر غير متاح', row['message_text'])

    @patch('bot.api')
    def test_invalid_receipts_never_become_confirmed_records(self, api):
        for result in ({}, {'message_id': True, 'date': 10, 'chat': {'id': 100}},
                       self.receipt(chat_id=200), self.receipt(date=False)):
            api.return_value = result
            with self.assertRaises(bot.TelegramError):
                self.deliver()
        self.assertEqual(self.store.report_rows(100), [])
        self.assertTrue(all(r[0] == 'uncertain' for r in self.store._db.execute('SELECT status FROM delivery_ledger')))

    @patch('bot.api')
    def test_ledger_write_failure_prevents_untracked_send(self, api):
        with patch.object(self.store, 'prepare_delivery', side_effect=OSError('storage failure')):
            with self.assertRaises(OSError):
                self.deliver()
        api.assert_not_called()

    @patch('bot.api')
    def test_rejected_pacing_never_creates_a_false_delivery(self, api):
        def reject():
            raise bot.TelegramError('stopped before send')
        with self.assertRaises(bot.TelegramError):
            self.deliver(before_send=reject)
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM delivery_ledger').fetchone()[0], 0)
        api.assert_not_called()

    @patch('bot.api')
    def test_prices_are_recorded_separately_for_each_actual_recipient(self, api):
        self.client.latest_price.side_effect = ['1.0', '1.01']
        api.side_effect = [self.receipt(), self.receipt(chat_id=200, message_id=21)]
        self.deliver(100)
        self.deliver(200)
        self.assertEqual(self.store.report_rows(100)[0]['quote_price'], '1.0')
        self.assertEqual(self.store.report_rows(200)[0]['quote_price'], '1.01')

    @patch('bot.api')
    def test_report_is_owner_only_even_for_approved_users_and_forwarded_identity(self, api):
        self.store.request_access(200, 'Visitor', 10)
        self.store.decide_access(200, True, 20)
        reports = Mock()
        for kwargs in ({'user': 200}, {'user': -100, 'kind': 'group', 'sender': 100},
                       {'user': 100, 'sender': 200}):
            self.command(reports=reports, **kwargs)
        reports.request.assert_not_called()
        self.command(reports=reports)
        reports.request.assert_called_once_with(100)

    @patch('bot.api')
    def test_failed_ack_does_not_schedule_report(self, api):
        api.side_effect = bot.TelegramError('transient', status=429)
        reports = Mock()
        with self.assertRaises(bot.TelegramError):
            self.command(reports=reports)
        reports.request.assert_not_called()

    @patch('bot.api')
    def test_command_update_routes_to_report_worker_and_advances_offset(self, api):
        update = {'update_id': 20, 'message': {'text': '/report',
                  'chat': {'id': 100, 'type': 'private'}, 'from': {'id': 100}}}
        reports = Mock()
        self.assertEqual(bot.process_updates('test-token', [update], self.store, reports=reports), 21)
        reports.request.assert_called_once_with(100)
        self.assertEqual(self.store.get_update_offset(), 21)

    def test_multipart_document_upload_contains_valid_fields_and_file_bytes(self):
        response = io.BytesIO(json.dumps({'ok': True, 'result': self.receipt()}).encode())
        with patch('bot.urlopen', return_value=response) as urlopen:
            bot.send_document('test-token', 100, b'PK-TEST-REPORT', 'signals-report.xlsx', 'تقرير')
        request = urlopen.call_args.args[0]
        header = ('Content-Type: ' + request.get_header('Content-type') + '\r\nMIME-Version: 1.0\r\n\r\n').encode()
        multipart = BytesParser(policy=default).parsebytes(header + request.data)
        fields = {part.get_param('name', header='content-disposition'): part for part in multipart.iter_parts()}
        self.assertEqual(fields['chat_id'].get_payload(decode=True), b'100')
        self.assertEqual(fields['caption'].get_payload(decode=True).decode(), 'تقرير')
        self.assertEqual(fields['document'].get_filename(), 'signals-report.xlsx')
        self.assertEqual(fields['document'].get_payload(decode=True), b'PK-TEST-REPORT')
        self.assertEqual(fields['document'].get_content_type(),
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_document_transport_errors_hide_token_and_preserve_rate_limit(self):
        with patch('bot.urlopen', side_effect=URLError('test-token must never be shown')):
            with self.assertRaises(bot.TelegramError) as caught:
                bot.send_document('test-token', 100, b'PK', 'report.xlsx')
        self.assertNotIn('test-token', str(caught.exception))
        error = HTTPError('https://api.telegram.org/bottest-token/sendDocument', 429, 'limited', {},
                          io.BytesIO(b'{"parameters":{"retry_after":123}}'))
        with patch('bot.urlopen', side_effect=error):
            with self.assertRaises(bot.TelegramError) as caught:
                bot.send_document('test-token', 100, b'PK', 'report.xlsx')
        self.assertEqual(caught.exception.retry_after, 123)
        self.assertNotIn('test-token', str(caught.exception))

    def test_document_filename_injection_is_rejected_before_network(self):
        with patch('bot.urlopen') as urlopen:
            with self.assertRaises(ValueError):
                bot.send_document('test-token', 100, b'PK', 'bad\r\nfilename.xlsx')
        urlopen.assert_not_called()

    @patch("approved_universe.configure_store")
    def test_production_startup_wires_ledger_sender_and_private_report_worker(self, configure):
        store = Mock()
        store.admin_id = 100
        store.is_admin.return_value = True
        store.get_update_offset.return_value = 0
        store.status.return_value = {'state': 'ready'}
        worker = Mock()
        worker.is_alive.return_value = False
        requests = []
        update = {'update_id': 20, 'message': {'text': '/report', 'chat': {'id': 100, 'type': 'private'}, 'from': {'id': 100}}}
        polls = iter([[update]])
        def api(token, method, payload=None):
            requests.append(method)
            if method == 'getWebhookInfo': return {'url': ''}
            if method == 'getUpdates':
                try: return next(polls)
                except StopIteration: raise KeyboardInterrupt
            return {}
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_ADMIN_ID': '100'}, clear=True), \
                patch('bot.api', side_effect=api), patch('alerts.AlertStore', return_value=store), \
                patch('alerts.Scanner') as scanner, patch('market.BinanceClient'), \
                patch('history.HistoryService') as history, patch('reporting.ReportService') as reports, \
                patch('bot.threading.Thread', return_value=worker):
            history.return_value.is_alive.return_value = False
            reports.return_value.is_alive.return_value = False
            self.assertEqual(bot.main(), 0)
            self.assertIn('send_signal', scanner.call_args.kwargs)
            self.assertTrue(callable(scanner.call_args.kwargs['send_signal']))
            reports.return_value.request.assert_called_once_with(100)
            reports.return_value.join.assert_called_once_with(timeout=5)
            store.configure_access.assert_called_once_with(100)
            store.close.assert_called_once()
        self.assertIn('setMyCommands', requests)


if __name__ == '__main__':
    unittest.main()
