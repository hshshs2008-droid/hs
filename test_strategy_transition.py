"""Rule updates preserve old receipts without releasing old queued signals."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alerts import AlertStore, Scanner
from gate_runtime import GateStore
from indicators import Signal
from market import Candle, FOUR_HOURS_MS


def signal(close):
    return Signal(close, 29, 31, 10, 18, 14, 16, close, close)


class StrategyTransitionTests(unittest.TestCase):
    def test_scanner_evaluates_only_new_completion_after_rule_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'main.sqlite3'
            store = AlertStore(path)
            store.subscribe(1, 0)
            store.record('ABCUSDT', 98 * FOUR_HOURS_MS - 1)
            store._set_metadata('strategy_version', 'previous-rsi20')
            store.close()
            with patch('alerts.time.time', return_value=100 * FOUR_HOURS_MS / 1000 + 1):
                store = AlertStore(path)
            try:
                candles = [Candle(i * FOUR_HOURS_MS, (i+1) * FOUR_HOURS_MS - 1, 10, 11, 9, 10)
                           for i in range(98, 101)]
                client = Mock()
                client.server_time.return_value = 101 * FOUR_HOURS_MS + 15000
                client.spot_symbols.return_value = ['ABCUSDT']
                client.candles.return_value = candles
                sender = Mock()
                scanner = Scanner(client, store, sender, request_delay=0)
                with patch('alerts.entry_signal', side_effect=lambda rows: signal(rows[-1].close_time)) as detect:
                    scanner.scan_once()
                self.assertEqual(detect.call_count, 1)
                self.assertEqual(detect.call_args.args[0][-1], candles[-1])
                self.assertEqual(sender.call_count, 1)
            finally:
                store.close()

    def test_transition_preserves_records_and_boundary_across_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'main.sqlite3'
            store = AlertStore(path)
            store.subscribe(1, 0)
            store.set_update_offset(123)
            store.record('SENTUSDT', 1000, signal(1000))
            store.delivery_attempt(1, 'SENTUSDT', 1000, sent_at_ms=2000)
            store.record('QUEUEDUSDT', 3000, signal(3000))
            store._set_metadata('strategy_version', 'previous-rsi20')
            store.close()
            with patch('alerts.time.time', return_value=10):
                store = AlertStore(path)
            self.assertEqual(store.strategy_activation_ms, 10000)
            self.assertEqual(store.pending(), [])
            self.assertFalse(store.is_pending(1, 'QUEUEDUSDT', 3000))
            self.assertEqual(store.get_update_offset(), 123)
            self.assertEqual(store.subscribers(), [(1, 0)])
            self.assertEqual(store._db.execute('SELECT count(*) FROM signals').fetchone()[0], 2)
            self.assertEqual(store._db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 2)
            self.assertEqual(len(store.report_rows(1)), 1)
            store.record('NEWUSDT', 11000, signal(11000))
            self.assertEqual([row[1] for row in store.pending()], ['NEWUSDT'])
            self.assertTrue(store.is_pending(1, 'NEWUSDT', 11000))
            store.close()
            with patch('alerts.time.time', return_value=20):
                store = AlertStore(path)
            try:
                self.assertEqual(store.strategy_activation_ms, 10000)
                self.assertEqual([row[1] for row in store.pending()], ['NEWUSDT'])
            finally:
                store.close()

    def test_gate_pending_obeys_strategy_boundary_and_preserves_membership(self):
        with tempfile.TemporaryDirectory() as folder:
            main = AlertStore(Path(folder) / 'main.sqlite3')
            main.subscribe(1, 0)
            path = Path(folder) / 'gate.sqlite3'
            gate = GateStore(path, main, activation_started_at_ms=0)
            gate.record('ABC_USDT', 3000, signal(3000))
            gate._set_metadata('strategy_version', 'previous-rsi20')
            gate.close()
            with patch('alerts.time.time', return_value=10):
                gate = GateStore(path, main)
            try:
                self.assertFalse(gate.is_pending(1, 'ABC_USDT', 3000))
                self.assertEqual(gate.pending(), [])
                self.assertEqual(gate.subscribers(), [(1, 0)])
                gate.record('ABC_USDT', 11000, signal(11000))
                self.assertTrue(gate.is_pending(1, 'ABC_USDT', 11000))
            finally:
                gate.close()
                main.close()
