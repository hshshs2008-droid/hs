"""Exchange isolation and shared membership operate on temporary databases."""
from dataclasses import asdict
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

from alerts import AlertStore, Scanner
from gate_runtime import CombinedHistory, GateStore, MarketRouter, ReportStore
from history import HISTORY_WINDOW_MS, HistoryResult, HistoryService, format_history
from indicators import Signal
from market import FOUR_HOURS_MS, MarketError


class GateRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.main = AlertStore(self.path / 'binance.sqlite3')
        self.addCleanup(self.main.close)
        self.main.configure_access(100)
        self.main.subscribe(100, 100)
        self.gate = GateStore(self.path / 'gate.sqlite3', self.main, activation_started_at_ms=0)
        self.addCleanup(self.gate.close)
        self.close = 100 * FOUR_HOURS_MS - 1
        self.now = (self.close + 15001) / 1000
        self.signal = Signal(self.close, 19, 21, 10, 23, 14, 21, self.close, self.close)

    def confirmed(self, store, symbol='ABC_USDT', price='1.25'):
        attempt = store.prepare_delivery(100, symbol, self.close, price, 1000, 'payload', 1001)
        store.confirm_delivery(attempt, 10, 2000, 2001)
        return attempt

    def test_gate_cannot_share_the_binance_database_file(self):
        with self.assertRaises(ValueError):
            GateStore(self.path / 'binance.sqlite3', self.main)
        self.assertEqual(self.main.subscribers(), [(100, 100)])

    def test_gate_membership_and_approval_come_from_main_store(self):
        self.main.request_access(200, 'Visitor', 100)
        self.assertFalse(self.gate.can_access(200))
        self.assertEqual(self.gate.subscribers(), [(100, 100)])
        self.main.decide_access(200, True, 200)
        self.assertTrue(self.gate.can_access(200))
        self.assertEqual(self.gate.subscribers(), [(100, 100), (200, 200)])
        self.main.revoke_access(200)
        self.assertFalse(self.gate.can_access(200))
        self.assertEqual(self.gate.subscribers(), [(100, 100)])
        self.assertTrue(self.gate.is_admin(100))
        self.assertTrue(self.gate.access_required())

    def test_isolated_native_pair_checkpoints_and_alert_platform(self):
        self.main.record('ABCUSDT', self.close, self.signal)
        self.gate.record('ABC_USDT', self.close, self.signal)
        self.assertEqual(self.main.checkpoint('ABCUSDT'), self.close)
        self.assertIsNone(self.main.checkpoint('ABC_USDT'))
        self.assertEqual(self.gate.checkpoint('ABC_USDT'), self.close)
        self.assertIsNone(self.gate.checkpoint('ABCUSDT'))
        self.assertIn('🏷️ Gate.io Spot', self.gate.pending()[0][3])
        self.assertIn('🏷️ Binance Spot', self.main.pending()[0][3])

    def test_unsubscribe_and_reapproval_cancel_gate_pending_without_deleting_ledger(self):
        self.gate.record('ABC_USDT', self.close, self.signal)
        self.assertTrue(self.gate.is_pending(100, 'ABC_USDT', self.close))
        self.confirmed(self.gate, symbol='OTHER_USDT')
        self.main.unsubscribe(100)
        self.assertFalse(self.gate.is_pending(100, 'ABC_USDT', self.close))
        self.assertEqual(self.gate.pending(), [])
        self.main.subscribe(100, 5000)
        self.assertFalse(self.gate.is_pending(100, 'ABC_USDT', self.close))
        self.assertEqual(self.gate.subscribers(), [(100, 5000)])
        self.assertEqual(self.gate.pending(), [])
        self.assertEqual(len(self.gate.report_rows(100)), 1)
        reopened = GateStore(self.path / 'gate.sqlite3', self.main, activation_started_at_ms=0)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.report_rows(100)[0]['quote_price'], '1.25')

    def test_native_punctuation_does_not_change_the_market_identity(self):
        for index, symbol in enumerate(('OLD-V2_USDT', 'TOKEN.V2_USDT'), 1):
            attempt = self.gate.prepare_delivery(100, symbol, self.close, '1.25', 1000, 'payload', 1001)
            self.gate.confirm_delivery(attempt, index, 2000, 2001)
        self.assertEqual({row['symbol'] for row in self.gate.report_rows(100)},
                         {'OLD-V2_USDT', 'TOKEN.V2_USDT'})

    def test_first_gate_enable_time_prevents_old_live_alert_replay(self):
        path = self.path / 'new-gate.sqlite3'
        with patch('gate_runtime.time.time', return_value=(self.close + 2000) / 1000):
            fresh = GateStore(path, self.main)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.subscribers(), [(100, self.close + 2000)])
        fresh.record('ABC_USDT', self.close, self.signal)
        self.assertEqual(fresh.pending(), [])
        reopened = GateStore(path, self.main, activation_started_at_ms=0)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.activation_started_at_ms, self.close + 2000)
        self.assertEqual(reopened.subscribers(), [(100, self.close + 2000)])
        later_close = self.close + FOUR_HOURS_MS
        later = Signal(later_close, 19, 21, 10, 23, 14, 21, later_close, later_close)
        reopened.record('ABC_USDT', later_close, later)
        self.assertTrue(reopened.is_pending(100, 'ABC_USDT', later_close))

    def test_refreshed_eligibility_filters_pending_but_keeps_confirmed_history(self):
        old_close = self.close - FOUR_HOURS_MS
        attempt = self.gate.prepare_delivery(100, 'DROP_USDT', old_close, '1.25', 1000, 'payload', 1001)
        self.gate.confirm_delivery(attempt, 10, 2000, 2001)
        self.gate.record('DROP_USDT', self.close, self.signal)
        self.gate.record('KEEP_USDT', self.close, self.signal)
        self.assertEqual(len(self.gate.pending()), 2)
        self.gate.eligible_symbol = lambda symbol: symbol == 'KEEP_USDT'
        self.assertFalse(self.gate.is_pending(100, 'DROP_USDT', self.close))
        self.assertTrue(self.gate.is_pending(100, 'KEEP_USDT', self.close))
        self.assertEqual([row[1] for row in self.gate.pending()], ['KEEP_USDT'])
        self.assertEqual([row['symbol'] for row in self.gate.report_rows(100)], ['DROP_USDT'])
        self.gate.eligible_symbol = lambda symbol: False  # No discovered universe yet.
        self.assertEqual(self.gate.pending(), [])
        self.assertEqual(len(self.gate.report_rows(100)), 1)
        self.gate.eligible_symbol = lambda symbol: True
        self.assertEqual(len(self.gate.pending()), 2)

    def test_signal_delivery_and_subscription_reconciliation_rollback_together(self):
        previous_close = self.close - FOUR_HOURS_MS
        self.gate.record('ABC_USDT', previous_close)
        with self.gate._db:
            self.gate._db.execute("""
                CREATE TRIGGER abort_pending_delivery BEFORE INSERT ON deliveries
                BEGIN SELECT RAISE(ABORT, 'test rejects delivery insert'); END
            """)
        self.assertEqual(self.gate._db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.gate.record('ABC_USDT', self.close, self.signal)
        self.assertEqual(self.gate._db.execute('SELECT COUNT(*) FROM signals').fetchone()[0], 0)
        self.assertEqual(self.gate._db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)
        self.assertEqual(self.gate._db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 0)
        self.assertEqual(self.gate.checkpoint('ABC_USDT'), previous_close)
        # A direct reconciliation still owns/commits its own transaction.
        self.assertEqual(self.gate.subscribers(), [(100, 100)])
        reopened = GateStore(self.path / 'gate.sqlite3', self.main)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened._db.execute('SELECT chat_id, activation_ms FROM subscriptions').fetchall(),
                         [(100, 100)])

    def test_telegram_pacing_and_cooldowns_are_shared(self):
        self.gate.set_delivery_blocked_until(12345)
        self.assertEqual(self.main.get_delivery_blocked_until(), 12345)
        self.main.set_next_delivery_at(54321)
        self.assertEqual(self.gate.get_next_delivery_at(), 54321)
        self.gate.set_next_delivery_at(65432)
        self.assertEqual(self.main.get_next_delivery_at(), 65432)

    def test_report_rows_keep_source_identity_and_prices_after_merging(self):
        self.confirmed(self.main, 'ABCUSDT', '2.00')
        self.confirmed(self.gate, 'ABC_USDT', '1.25')
        report = ReportStore(self.main, self.gate)
        self.assertTrue(report.is_admin(100))
        self.assertFalse(report.is_admin(200))
        rows = report.report_rows(100)
        self.assertEqual([r['quote_key'] for r in rows], [('binance', 'ABCUSDT'), ('gate', 'ABC_USDT')])
        self.assertEqual([r['quote_price'] for r in rows], ['2.00', '1.25'])
        self.assertEqual([r['exchange_name'] for r in rows], ['Binance Spot', 'Gate.io Spot'])
        self.assertIsInstance(report.get_ledger_started_at_ms(), int)
        self.assertEqual(report.report_rows(200), [])

    def test_router_never_substitutes_another_platforms_price(self):
        binance, gate = Mock(), Mock()
        binance.latest_price.return_value = '2.00'
        gate.latest_price.return_value = '1.25'
        router = MarketRouter(binance, gate)
        self.assertEqual(router.latest_price(('gate', 'ABC_USDT')), '1.25')
        gate.latest_price.assert_called_once_with('ABC_USDT')
        binance.latest_price.assert_not_called()
        self.assertEqual(router.latest_price(('binance', 'ABCUSDT')), '2.00')
        self.assertEqual(router.latest_price('ABCUSDT'), '2.00')
        with self.assertRaises(MarketError):
            MarketRouter(binance).latest_price(('gate', 'ABC_USDT'))
        with self.assertRaises(MarketError):
            router.latest_price(('unknown', 'ABCUSDT'))
        with self.assertRaises(MarketError):
            router.latest_price(('gate',))

    def test_gate_history_native_symbol_label_platform_and_reopen(self):
        client = Mock()
        client.server_time.return_value = int(self.now * 1000)
        client.spot_symbols.return_value = ['ABC_USDT']
        from market import Candle
        client.candles.return_value = [Candle(self.close - FOUR_HOURS_MS + 1, self.close, 1, 2, .5, 1)]
        service = HistoryService(client, Mock(), threading.Event(), request_delay=0,
                                 clock=lambda: self.now, store=self.gate)
        with patch('history.historical_signals', return_value=[self.signal]):
            result = service.scan_once()
        self.assertEqual(result.exchange_name, 'Gate.io Spot')
        text = format_history(result)[0]
        self.assertIn('Gate.io Spot', text)
        self.assertIn('<code>ABC/USDT</code>', text)
        self.assertNotIn('Binance Spot', text)
        restored = HistoryService(Mock(), Mock(), threading.Event(), clock=lambda: self.now,
                                  store=self.gate)
        self.assertEqual(restored.cached_result().matches, result.matches)
        self.assertEqual(restored.cached_result().exchange_name, 'Gate.io Spot')

    def test_wrong_platform_cache_is_not_reused(self):
        result = HistoryResult(int(self.now * 1000), self.close, 1, 1, (), True, {})
        self.gate.save_history_cache('complete', {'version': 2, 'window_ms': HISTORY_WINDOW_MS,
            'result': asdict(result), 'cache_key': [self.close, ['ABC_USDT']]})
        with self.assertLogs('history', level='WARNING'):
            service = HistoryService(Mock(), Mock(), threading.Event(), store=self.gate,
                                     clock=lambda: self.now)
        self.assertIsNone(service.cached_result())

    def test_empty_eligible_gate_universe_is_complete_and_ready(self):
        client = Mock()
        client.allow_empty_universe = True
        client.server_time.return_value = int(self.now * 1000)
        client.spot_symbols.return_value = []
        scanner = Scanner(client, self.gate, Mock(), request_delay=0, clock=lambda: self.now)
        status = scanner.scan_once()
        self.assertEqual(status['state'], 'ready')
        self.assertEqual(status['symbols_total'], 0)
        client.candles.assert_not_called()
        history = HistoryService(client, Mock(), threading.Event(), request_delay=0,
                                 clock=lambda: self.now, store=self.gate)
        result = history.scan_once()
        self.assertTrue(result.complete)
        self.assertEqual(result.exchange_name, 'Gate.io Spot')
        self.assertEqual(result.symbols_total, 0)

    def test_gate_failure_status_names_gate(self):
        client = Mock()
        client.server_time.side_effect = MarketError('unavailable', status=429)
        scanner = Scanner(client, self.gate, Mock(), request_delay=0, clock=lambda: self.now)
        status = scanner.scan_once()
        self.assertIn('Gate.io Spot', status['error'])
        self.assertNotIn('بايننس', status['error'])


class CombinedHistoryTests(unittest.TestCase):
    def test_both_sources_are_scheduled_even_if_first_returns_true(self):
        primary, secondary = Mock(), Mock()
        primary.request.return_value = primary.start_background.return_value = True
        secondary.request.return_value = secondary.start_background.return_value = True
        primary.cached_result.return_value = None
        secondary.cached_result.return_value = 'Gate cached'
        combined = CombinedHistory(primary, secondary)
        self.assertTrue(combined.request(100))
        primary.request.assert_called_once_with(100)
        secondary.request.assert_called_once_with(100)
        self.assertEqual(combined.cached_result(), 'Gate cached')
        self.assertTrue(combined.start_background())
        primary.start_background.assert_called_once()
        secondary.start_background.assert_called_once()
        primary.last_error = None
        secondary.last_error = 'delivery failed'
        primary.refresh_error = secondary.refresh_error = None
        self.assertEqual(combined.last_error, 'Gate.io Spot: delivery failed')
        self.assertIsNone(combined.refresh_error)
        combined.join(0)
        primary.join.assert_called_once_with(0)
        secondary.join.assert_called_once_with(0)

    def test_single_source_keeps_existing_behavior(self):
        primary = Mock()
        primary.cached_result.return_value = 'cached'
        primary.last_error = primary.refresh_error = None
        combined = CombinedHistory(primary)
        self.assertEqual(combined.cached_result(), 'cached')
        self.assertIsNone(combined.last_error)
        self.assertIsNone(combined.refresh_error)


if __name__ == '__main__':
    unittest.main()
