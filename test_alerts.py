import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from alerts import (AlertDeliveryError, AlertStore, CLOSE_GRACE_MS,
                    DELIVERY_SPACING_SECONDS, Scanner, format_alert)
from indicators import Signal
from market import Candle, FOUR_HOURS_MS, MarketError


def candles_ending(close_time, count=25):
    first = close_time - (count - 1) * FOUR_HOURS_MS
    return [Candle(end - FOUR_HOURS_MS + 1, end, 10, 11, 9, 10)
            for end in range(first, close_time + 1, FOUR_HOURS_MS)]


def signal_at(close_time, adjacent=False):
    return Signal(close_time, 19, 21, 10, 22, 14, 20,
                  close_time - FOUR_HOURS_MS if adjacent else close_time,
                  close_time)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state" / "alerts.sqlite3"
        self.store = AlertStore(self.path)
        self.addCleanup(self.store.close)
        self.target = 100 * FOUR_HOURS_MS - 1
        self.server_time = self.target + 1 + CLOSE_GRACE_MS
        self.now = self.server_time / 1000
        self.client = Mock()
        self.client.server_time.side_effect = lambda: self.server_time
        self.client.spot_symbols.return_value = ["BTCUSDT"]
        self.client.candles.side_effect = lambda *args: candles_ending(self.target)
        self.sender = Mock()
        self.scanner = self.make_scanner()

    def make_scanner(self, store=None):
        return Scanner(self.client, store or self.store, self.sender,
                       request_delay=0, clock=lambda: self.now,
                       sleep=lambda seconds: setattr(self, "now", self.now + seconds))

    def activate(self, chat_id=1):
        self.store.subscribe(chat_id, self.target - 1000)

    def detector(self, candles):
        return signal_at(self.target) if candles[-1].close_time == self.target else None

    def test_no_subscribers_means_no_network_requests(self):
        self.assertEqual(self.scanner.scan_once()["state"], "idle")
        self.client.server_time.assert_not_called()
        self.client.spot_symbols.assert_not_called()
        self.client.candles.assert_not_called()

    def test_subscriptions_are_idempotent_and_offset_survives_restart(self):
        self.assertTrue(self.store.subscribe(1, 100))
        self.assertFalse(self.store.subscribe(1, 200))
        self.assertEqual(self.store.subscribers(), [(1, 100)])
        self.store.set_update_offset(12)
        self.store.set_update_offset(8)
        self.store.close()
        restored = AlertStore(self.path)
        self.addCleanup(restored.close)
        self.assertEqual(restored.get_update_offset(), 12)
        self.assertEqual(restored.subscribers(), [(1, 100)])

    @patch("alerts.entry_signal")
    def test_multi_subscriber_delivery_deduplicated_across_poll_and_restart(self, detector):
        detector.side_effect = self.detector
        self.activate(1)
        self.activate(2)
        result = self.scanner.scan_once()
        self.assertEqual(result["alerts_sent"], 2)
        self.assertEqual({call.args[0] for call in self.sender.call_args_list}, {1, 2})
        self.assertEqual(self.store.checkpoint("BTCUSDT"), self.target)
        self.assertEqual(self.scanner.scan_once()["alerts_sent"], 0)
        self.assertEqual(self.client.candles.call_count, 1)
        self.store.close()
        restored = AlertStore(self.path)
        self.addCleanup(restored.close)
        self.assertEqual(self.make_scanner(restored).scan_once()["alerts_sent"], 0)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.client.candles.call_count, 1)

    @patch("alerts.entry_signal")
    def test_failed_delivery_survives_restart_and_retries_once_next_poll(self, detector):
        detector.side_effect = self.detector
        self.activate()
        self.sender.side_effect = [AlertDeliveryError("delivery unavailable"), None]
        first = self.scanner.scan_once()
        self.assertEqual((first["alerts_sent"], first["pending"]), (0, 1))
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.store.checkpoint("BTCUSDT"), self.target)
        self.store.close()
        restored = AlertStore(self.path)
        self.addCleanup(restored.close)
        second = self.make_scanner(restored).scan_once()
        self.assertEqual((second["alerts_sent"], second["pending"]), (1, 0))
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.client.candles.call_count, 1)

    def test_unsubscribe_discards_pending_and_reactivation_starts_fresh(self):
        self.activate()
        self.store.record("BTCUSDT", self.target, signal_at(self.target))
        self.assertEqual(len(self.store.pending()), 1)
        self.assertTrue(self.store.unsubscribe(1))
        self.assertFalse(self.store.unsubscribe(1))
        self.assertEqual(self.store.pending(), [])
        self.store.subscribe(1, self.target + 1000)
        self.store.record("BTCUSDT", self.target, signal_at(self.target))
        self.assertEqual(self.store.pending(), [])

    @patch("alerts.entry_signal")
    def test_signal_before_activation_never_sent_to_new_subscriber(self, detector):
        detector.side_effect = self.detector
        self.activate(1)
        self.store.subscribe(2, self.server_time)
        self.scanner.scan_once()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.sender.call_args.args[0], 1)
        self.assertEqual(self.store.checkpoint("BTCUSDT"), self.target)

    @patch("alerts.entry_signal")
    def test_fifteen_second_grace_and_open_candle_are_not_evaluated(self, detector):
        self.activate()
        detector.return_value = None
        recent_boundary = self.target + 1
        self.server_time = recent_boundary + CLOSE_GRACE_MS - 1
        self.client.candles.side_effect = lambda *args: candles_ending(self.target + FOUR_HOURS_MS)
        result = self.scanner.scan_once()
        self.assertEqual(result["target_close_time"], self.target - FOUR_HOURS_MS)
        self.assertEqual(self.store.checkpoint("BTCUSDT"), self.target - FOUR_HOURS_MS)
        detector.assert_not_called()  # All eligible candles precede subscription.
        self.server_time += 1
        self.scanner.scan_once()
        self.assertEqual(self.store.checkpoint("BTCUSDT"), self.target)
        self.assertEqual(detector.call_args.args[0][-1].close_time, self.target)

    @patch("alerts.entry_signal")
    def test_rate_limit_stops_scan_honors_long_retry_after_and_preserves_partial_progress(self, detector):
        detector.return_value = None
        self.activate()
        self.client.spot_symbols.return_value = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        self.client.candles.side_effect = [candles_ending(self.target),
                                           MarketError("rate limited", status=429, retry_after=10000)]
        first = self.scanner.scan_once()
        self.assertEqual(first["state"], "blocked")
        self.assertEqual(first["blocked_until"], self.now + 10000)
        self.assertEqual(self.store.checkpoint("AAAUSDT"), self.target)
        self.assertIsNone(self.store.checkpoint("BBBUSDT"))
        self.assertEqual(self.client.candles.call_count, 2)
        calls = self.client.server_time.call_count
        self.now += 9999
        self.assertEqual(self.scanner.scan_once()["state"], "backoff")
        self.assertEqual(self.client.server_time.call_count, calls)
        self.now += 1
        self.client.candles.side_effect = lambda *args: candles_ending(self.target)
        self.assertEqual(self.scanner.scan_once()["state"], "ready")
        self.assertEqual(self.client.candles.call_count, 4)
        self.assertEqual(self.store.checkpoint("CCCUSDT"), self.target)

    @patch("alerts.entry_signal")
    def test_missing_latest_candle_is_retried_and_missed_signal_preserved(self, detector):
        self.activate()
        detector.side_effect = self.detector
        original_target = self.target
        self.client.candles.side_effect = lambda *args: candles_ending(self.target - FOUR_HOURS_MS)
        self.assertEqual(self.scanner.scan_once()["state"], "blocked")
        self.assertIsNone(self.store.checkpoint("BTCUSDT"))
        self.target += FOUR_HOURS_MS
        self.server_time += FOUR_HOURS_MS
        self.now += FOUR_HOURS_MS / 1000
        self.client.candles.side_effect = lambda *args: candles_ending(self.target)
        detector.side_effect = lambda rows: signal_at(original_target) if rows[-1].close_time == original_target else None
        self.assertEqual(self.scanner.scan_once()["alerts_sent"], 1)
        self.assertEqual(self.store.checkpoint("BTCUSDT"), self.target)

    def test_unexpected_delivery_failure_exposes_safe_status_and_stop_interrupts_retry(self):
        self.activate()
        self.store.record("BTCUSDT", self.target, signal_at(self.target))
        self.sender.side_effect = TypeError("programming error")
        stop = threading.Event()
        waits = []

        def interrupt_retry(seconds):
            waits.append(seconds)
            stop.set()
            return True

        with patch.object(stop, "wait", side_effect=interrupt_retry), self.assertLogs("alerts", level="ERROR") as logs:
            self.scanner.run(stop)
        status = self.store.status()
        self.assertEqual(status["state"], "error")
        self.assertNotIn("programming error", status["error"])
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(waits, [30])
        self.assertEqual(status["last_error_ms"], int(self.now * 1000))
        self.assertEqual(status["retry_at_ms"], int(self.now * 1000) + 30_000)
        self.assertIn("TypeError", logs.output[0])
        self.assertNotIn("programming error", "\n".join(logs.output))
        self.assertIsNone(self.scanner._stop)

    def test_worker_recovers_after_unexpected_failure_and_preserves_pending_alert(self):
        self.activate()
        self.store.record("BTCUSDT", self.target, signal_at(self.target))
        self.sender.side_effect = [TypeError("private request detail"), None]
        stop = threading.Event()
        waits = []

        def advance_or_stop(seconds):
            waits.append(seconds)
            self.now += seconds
            if len(waits) == 2:
                stop.set()
                return True
            return False

        with patch.object(stop, "wait", side_effect=advance_or_stop), self.assertLogs("alerts", level="ERROR") as logs:
            self.scanner.run(stop)
        state = self.store.status()
        self.assertEqual(waits, [30, self.scanner.poll_seconds])
        self.assertEqual(state["state"], "ready")
        self.assertEqual((state["alerts_sent"], state["pending"]), (1, 0))
        self.assertEqual(self.sender.call_count, 2)
        self.assertIsNotNone(state["last_error_ms"])
        self.assertGreater(state["heartbeat_ms"], state["last_error_ms"])
        self.assertEqual(state["retry_at_ms"], 0)
        self.assertNotIn("private request detail", "\n".join(logs.output))

    def test_repeated_unexpected_failures_use_bounded_retry_without_spinning(self):
        stop = threading.Event()
        waits = []

        def stop_after_three_waits(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                stop.set()

        with patch.object(self.scanner, "scan_once", side_effect=ValueError("private")) as scan, \
                patch.object(stop, "wait", side_effect=stop_after_three_waits), \
                self.assertLogs("alerts", level="ERROR"):
            self.scanner.run(stop)
        self.assertEqual(waits, [30, 60, 60])
        self.assertEqual(scan.call_count, 3)

    def test_shared_market_cooldown_deadline_is_not_extended(self):
        self.activate()
        error = MarketError("shared cooldown", status=429, retry_after=5)
        error.cooldown_until = self.now + 5
        self.client.server_time.side_effect = error
        self.assertEqual(self.scanner.scan_once()["blocked_until"], self.now + 5)
        self.now += 4
        self.assertEqual(self.scanner.scan_once()["state"], "backoff")
        self.assertEqual(self.client.server_time.call_count, 1)
        self.now += 1
        self.client.server_time.side_effect = lambda: self.server_time
        self.assertEqual(self.scanner.scan_once()["state"], "ready")

    def test_telegram_long_cooldown_persists_after_restart_while_market_scanning_continues(self):
        self.activate(1)
        self.activate(2)
        self.store.record("BTCUSDT", self.target, signal_at(self.target))
        self.sender.side_effect = AlertDeliveryError("rate limited", status=429, retry_after=10000)
        deadline = self.now + 10000
        first = self.scanner.scan_once()
        self.assertEqual(first["delivery_blocked_until"], deadline)
        self.assertEqual(first["pending"], 2)
        self.assertIn("مهلة تيليجرام", first["delivery_warning"])
        self.assertEqual(self.sender.call_count, 1)
        self.client.server_time.assert_called_once()
        self.store.close()
        restored = AlertStore(self.path)
        self.addCleanup(restored.close)
        scanner = self.make_scanner(restored)
        self.now = deadline - 1
        self.assertEqual(scanner.scan_once()["pending"], 2)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.client.server_time.call_count, 2)
        self.now = deadline
        self.sender.side_effect = None
        result = scanner.scan_once()
        self.assertEqual((result["alerts_sent"], result["pending"]), (2, 0))
        self.assertIsNone(result["delivery_warning"])

    def test_alert_bursts_are_spaced_globally_and_across_restart(self):
        self.activate(1)
        self.activate(2)
        for symbol in ("BTCUSDT", "ETHUSDT"):
            self.store.record(symbol, self.target, signal_at(self.target))
        attempts = []
        self.sender.side_effect = lambda *args: attempts.append(self.now)
        self.assertEqual(self.scanner.scan_once()["alerts_sent"], 4)
        self.assertEqual(len(attempts), 4)
        for first, second in zip(attempts, attempts[1:]):
            self.assertGreaterEqual(second - first + 1e-9, DELIVERY_SPACING_SECONDS)
        self.store.record("SOLUSDT", self.target, signal_at(self.target))
        self.store.close()
        restored = AlertStore(self.path)
        self.addCleanup(restored.close)
        self.assertEqual(self.make_scanner(restored).scan_once()["alerts_sent"], 2)
        self.assertGreaterEqual(attempts[4] - attempts[3] + 1e-9, DELIVERY_SPACING_SECONDS)

    def test_telegram_429_without_retry_after_uses_sixty_second_cooldown(self):
        self.activate()
        self.store.record("BTCUSDT", self.target, signal_at(self.target))
        self.sender.side_effect = AlertDeliveryError("rate limited", status=429)
        deadline = self.now + 60
        self.assertEqual(self.scanner.scan_once()["delivery_blocked_until"], deadline)

    def test_requests_are_serially_paced_and_adjacent_signal_time_is_clear(self):
        self.activate()
        delays = []
        scanner = Scanner(self.client, self.store, self.sender,
                          clock=lambda: self.now, sleep=delays.append)
        scanner.scan_once()
        self.assertEqual(delays, [0.15, 0.15, 0.15])
        text = format_alert("BTCUSDT", signal_at(self.target, adjacent=True))
        self.assertEqual(text.splitlines()[0], "📈 BTCUSDT")
        self.assertIn("🏷️ Binance Spot", text.splitlines())
        self.assertIn("⏱️ 4h • شموع مغلقة", text.splitlines())
        self.assertNotIn("قيم آخر إغلاق", text)
        self.assertIn("شمعتين متتاليتين", text)
        self.assertNotIn("بتوقيت", text)
        self.assertIn("RSI (14): 19.00 → 21.00", text)

    def test_israel_timezone_handles_winter_summer_and_both_dst_transitions(self):
        cases = [
            ("2026-01-15T12:00:00+00:00", "2026-01-15 (14:00)"),
            ("2026-07-15T12:00:00+00:00", "2026-07-15 (15:00)"),
            ("2026-03-26T23:59:00+00:00", "2026-03-27 (01:59)"),
            ("2026-03-27T00:00:00+00:00", "2026-03-27 (03:00)"),
            ("2026-10-24T22:59:00+00:00", "2026-10-25 (01:59)"),
            ("2026-10-24T23:00:00+00:00", "2026-10-25 (01:00)"),
        ]
        for utc_time, expected in cases:
            with self.subTest(utc_time=utc_time):
                close_time = int(datetime.fromisoformat(utc_time).timestamp() * 1000) - 1
                text = format_alert("BTCUSDT", signal_at(close_time))
                self.assertIn(f"🕒 إغلاق شمعة 4h: {expected}", text.splitlines())
                self.assertIn(f"عبور RSI فوق 30: {expected}", text)
                self.assertIn(f"تقاطع K فوق D: {expected}", text)

    def test_legacy_queued_message_times_are_refreshed_without_database_mutation(self):
        close_time = int(datetime.fromisoformat("2026-01-15T12:00:00+00:00").timestamp() * 1000) - 1
        self.store.subscribe(1, 0)
        self.store.record("BTCUSDT", close_time, signal_at(close_time))
        expected = format_alert("BTCUSDT", signal_at(close_time)).replace("عبور RSI فوق 30:", "عبور RSI فوق 20:")
        legacy = (
            "🔔 تحقق فلتر الدخول: BTCUSDT\n"
            "بايننس سبوت • فريم 4 ساعات • شموع مغلقة\n"
            "إغلاق الإشارة: 2026-01-15 15:00 (بتوقيت الرياض)\n"
            "قيم آخر إغلاق:\n"
            "RSI (14): 19.00 → 21.00\n"
            "K: 10.00 → 22.00\n"
            "D: 14.00 → 20.00\n"
            "عبور RSI فوق 20: 2026-01-15 15:00\n"
            "تقاطع K فوق D: 2026-01-15 15:00\n"
            "الشرطان في الشمعة نفسها."
        )
        with self.store._lock, self.store._db:
            self.store._db.execute("UPDATE signals SET message = ? WHERE symbol = ? AND close_time = ?",
                                   (legacy, "BTCUSDT", close_time))
        queued = self.store.pending()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0], (1, "BTCUSDT", close_time, expected))
        self.assertEqual(self.store.pending("BTCUSDT"), queued)
        self.assertEqual(self.store.checkpoint("BTCUSDT"), close_time)
        with self.store._lock:
            stored = self.store._db.execute("SELECT message FROM signals WHERE symbol = ?", ("BTCUSDT",)).fetchone()[0]
            delivery = self.store._db.execute("SELECT sent_at_ms, attempts FROM deliveries").fetchone()
        self.assertEqual(stored, legacy)
        self.assertEqual(delivery, (None, 0))


if __name__ == "__main__":
    unittest.main()
