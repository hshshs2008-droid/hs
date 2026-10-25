"""Durable quote history is independent of subscription lifecycle."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from alerts import AlertStore, Scanner
from indicators import Signal


class DeliveryLedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'alerts.sqlite3'
        self.store = AlertStore(self.path)
        self.addCleanup(self.store.close)

    def prepare(self, chat=101, close=1000, price='0.000000012345678901', quote_at=2000,
                attempted=2001):
        return self.store.prepare_delivery(chat, 'BTCUSDT', close, price, quote_at,
                                           'exact displayed payload', attempted)

    def confirm(self, attempt, message_id=10, sent=3000, confirmed=3001):
        self.store.confirm_delivery(attempt, message_id, sent, confirmed)

    def signal(self, close):
        return Signal(close, 19, 21, 10, 23, 14, 21, close, close)

    def test_quote_precision_payload_and_receipt_survive_reopen(self):
        attempt = self.prepare()
        self.assertEqual(self.store.report_rows(101), [])
        self.confirm(attempt)
        reopened = AlertStore(self.path)
        self.addCleanup(reopened.close)
        row, = reopened.report_rows(101)
        self.assertEqual(row['quote_price'], '0.000000012345678901')
        self.assertEqual(row['quote_at_ms'], 2000)
        self.assertEqual(row['message_text'], 'exact displayed payload')
        self.assertEqual(row['telegram_message_id'], 10)
        self.assertEqual(row['telegram_sent_at_ms'], 3000)
        self.assertEqual(row['confirmed_at_ms'], 3001)
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['origin'], 'live')
        self.assertEqual(reopened.get_ledger_started_at_ms(), self.store.get_ledger_started_at_ms())

    def test_each_recipient_and_repeated_coin_signal_has_its_own_quote(self):
        first = self.prepare(chat=101, close=1000, price='1.00')
        other = self.prepare(chat=102, close=1000, price='1.01')
        later = self.prepare(chat=101, close=5000, price='1.20')
        for index, attempt in enumerate((first, other, later), 10):
            self.confirm(attempt, message_id=index)
        self.assertEqual([r['quote_price'] for r in self.store.report_rows(101)], ['1.00', '1.20'])
        self.assertEqual([r['quote_price'] for r in self.store.report_rows(102)], ['1.01'])

    def test_failed_uncertain_and_prepared_attempts_do_not_appear_as_sent(self):
        failed = self.prepare(price='1.0')
        uncertain = self.prepare(price='1.1')
        prepared = self.prepare(price='1.2')
        self.store.fail_delivery(failed, 429)
        self.store.fail_delivery(uncertain, uncertain=True)
        self.assertEqual(self.store.report_rows(101), [])
        successful = self.prepare(price='1.3')
        self.confirm(successful)
        self.assertEqual(self.store.report_rows(101)[0]['quote_price'], '1.3')
        statuses = dict(self.store._db.execute('SELECT attempt_id, status FROM delivery_ledger'))
        self.assertEqual(statuses[failed], 'failed')
        self.assertEqual(statuses[uncertain], 'uncertain')
        self.assertEqual(statuses[prepared], 'prepared')

    def test_duplicate_successful_sends_keep_first_received_price(self):
        first = self.prepare(price=None, quote_at=None)
        second = self.prepare(price='1.5', attempted=4000)
        self.confirm(second, message_id=11, sent=5000)
        self.confirm(first, message_id=10, sent=3000)
        row, = self.store.report_rows(101)
        self.assertEqual(row['attempt_id'], first)
        self.assertIsNone(row['quote_price'])
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM delivery_ledger').fetchone()[0], 2)

    def test_history_survives_unsubscribe_revoke_and_reapproval(self):
        self.store.configure_access(100)
        self.store.request_access(101, 'Visitor', 1)
        self.store.decide_access(101, True, 500)
        self.store.record('BTCUSDT', 1000, self.signal(1000))
        attempt = self.prepare()
        self.confirm(attempt)
        self.store.unsubscribe(101)
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)
        self.assertEqual(len(self.store.report_rows(101)), 1)
        self.store.revoke_access(101)
        self.assertEqual(len(self.store.report_rows(101)), 1)
        self.store.decide_access(101, True, 6000, reconsider=True)
        self.assertEqual(len(self.store.report_rows(101)), 1)

    def test_confirmation_marks_existing_pending_delivery_atomically(self):
        self.store.subscribe(101, 100)
        self.store.record('BTCUSDT', 1000, self.signal(1000))
        attempt = self.prepare()
        self.confirm(attempt)
        self.assertEqual(self.store.pending(), [])
        # Scanner's normal completion must not increment attempts twice.
        self.store.delivery_attempt(101, 'BTCUSDT', 1000, sent_at_ms=4000)
        row = self.store._db.execute('SELECT sent_at_ms, attempts FROM deliveries').fetchone()
        self.assertEqual(row, (3000, 1))
        reopened = AlertStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.pending(), [])
        self.assertEqual(len(reopened.report_rows(101)), 1)
        self.assertEqual(reopened.report_rows(101)[0]['origin'], 'live')

    def test_confirmation_is_idempotent_and_cannot_be_overwritten_or_downgraded(self):
        attempt = self.prepare()
        self.confirm(attempt)
        self.confirm(attempt, confirmed=4000)
        self.store.fail_delivery(attempt, 500, uncertain=True)
        self.assertEqual(self.store.report_rows(101)[0]['confirmed_at_ms'], 3001)
        with self.assertRaises(ValueError):
            self.confirm(attempt, message_id=11)
        failed = self.prepare(close=5000)
        self.store.fail_delivery(failed, 403)
        with self.assertRaises(ValueError):
            self.confirm(failed)

    def test_legacy_migration_retains_confirmed_unknown_prices_without_changing_other_state(self):
        self.store.subscribe(101, 100)
        self.store.record('BTCUSDT', 1000, self.signal(1000))
        self.store.delivery_attempt(101, 'BTCUSDT', 1000, sent_at_ms=3000)
        self.store.set_update_offset(42)
        # Reopen exactly as an upgrade from the old schema would do.
        reopened = AlertStore(self.path)
        self.addCleanup(reopened.close)
        row, = reopened.report_rows(101)
        self.assertEqual(row['origin'], 'legacy')
        self.assertIsNone(row['quote_price'])
        self.assertIsNone(row['telegram_message_id'])
        self.assertEqual(row['telegram_sent_at_ms'], 3000)
        self.assertEqual(reopened.subscribers(), [(101, 100)])
        self.assertEqual(reopened.checkpoint('BTCUSDT'), 1000)
        self.assertEqual(reopened.get_update_offset(), 42)
        again = AlertStore(self.path)
        self.addCleanup(again.close)
        self.assertEqual(len(again.report_rows(101)), 1)
        self.assertEqual(again._db.execute('SELECT COUNT(*) FROM delivery_ledger').fetchone()[0], 1)
        self.assertEqual(again._db.execute('PRAGMA foreign_key_list(delivery_ledger)').fetchall(), [])

    def test_unknown_quote_is_saved_without_substituting_a_candle_price(self):
        attempt = self.prepare(price=None, quote_at=None)
        self.confirm(attempt)
        row, = self.store.report_rows(101)
        self.assertIsNone(row['quote_price'])
        self.assertIsNone(row['quote_at_ms'])

    def test_invalid_values_cannot_create_ledger_rows(self):
        base = dict(chat_id=101, symbol='BTCUSDT', signal_close_time=1000,
                    quote_price='1.0', quote_at_ms=2000, message_text='payload', attempted_at_ms=2001)
        cases = [('chat_id', True), ('chat_id', -1), ('chat_id', 2**52),
                 ('symbol', 'BTC/USDT'), ('signal_close_time', 1.5), ('quote_at_ms', True),
                 ('quote_price', float('nan')), ('quote_price', 'NaN'), ('quote_price', 'Infinity'),
                 ('quote_price', '-1'), ('quote_price', '0'), ('quote_price', 'not a price'),
                 ('quote_price', '1e999'), ('quote_at_ms', None), ('message_text', ''),
                 ('attempted_at_ms', -1)]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.store.prepare_delivery(**dict(base, **{key: value}))
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM delivery_ledger').fetchone()[0], 0)

    def test_invalid_telegram_receipts_are_not_confirmed(self):
        attempt = self.prepare()
        for args in ((True, 3000, 3001), (0, 3000, 3001), (10, -1, 3001), (10, 3000, 1.5)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.confirm_delivery(attempt, *args)
        self.assertEqual(self.store.report_rows(101), [])

    def test_explicit_scanner_sender_receives_signal_identity(self):
        self.store.subscribe(101, 100)
        self.store.record('BTCUSDT', 1000, self.signal(1000))
        old_sender = Mock()
        precise_sender = Mock()
        scanner = Scanner(Mock(), self.store, old_sender, send_signal=precise_sender,
                          request_delay=0, clock=lambda: 10)
        sent, failed = scanner._deliver(self.store.pending(), set())
        self.assertEqual((sent, failed), (1, 0))
        old_sender.assert_not_called()
        self.assertEqual(precise_sender.call_args.args[:3], (101, 'BTCUSDT', 1000))
        self.assertTrue(precise_sender.call_args.args[3].startswith('📈 BTCUSDT'))


if __name__ == '__main__':
    unittest.main()
