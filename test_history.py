from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
import threading
import time
import unittest
from unittest.mock import Mock, patch

from alerts import AlertDeliveryError, CLOSE_GRACE_MS
from history import DAY_MS, HistoryResult, HistoryService, format_history, historical_signals
from indicators import Signal
from market import Candle, FOUR_HOURS_MS, MarketError


def candles_ending(end, count=30):
    return [Candle(close - FOUR_HOURS_MS + 1, close, 10, 11, 9, 10)
            for close in range(end - (count - 1) * FOUR_HOURS_MS, end + 1, FOUR_HOURS_MS)]


def signal_at(end):
    return Signal(end, 19, 21, 10, 23, 14, 21, end, end)


class ReportText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.tags = []

    def handle_data(self, text):
        self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def visible_report(page):
    parser = ReportText()
    parser.feed(page)
    return "".join(parser.parts)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.target = 100 * FOUR_HOURS_MS - 1
        self.server_time = self.target + 1 + CLOSE_GRACE_MS
        self.now = self.server_time / 1000
        self.stop = threading.Event()
        self.client = Mock()
        self.client.server_time.side_effect = lambda: self.server_time
        self.client.spot_symbols.return_value = ["BTCUSDT"]
        self.client.candles.side_effect = lambda *args: candles_ending(self.target)
        self.sender = Mock()
        self.service = HistoryService(self.client, self.sender, self.stop, request_delay=0,
                                      clock=lambda: self.now, sleep=self.advance)
        self.addCleanup(self.shutdown)

    def advance(self, seconds):
        self.now += seconds

    def shutdown(self):
        self.stop.set()
        self.service.join(timeout=2)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("History worker did not finish in time.")

    @patch("history.entry_signal")
    def test_exact_24_hour_boundary_excludes_old_and_future_entries(self, detector):
        as_of = 3 * DAY_MS
        cutoff = as_of - DAY_MS
        times = [cutoff - 1, cutoff, cutoff + 1, as_of, as_of + 1]
        candles = [Candle(end - 1, end, 10, 11, 9, 10) for end in times]
        detector.side_effect = lambda prefix: signal_at(prefix[-1].close_time)
        result = historical_signals(candles, as_of)
        self.assertEqual([signal.close_time for signal in result], [cutoff + 1, as_of])
        self.assertEqual(detector.call_count, 2)
        self.assertEqual(detector.call_args.args[0][-1].close_time, as_of)

    def test_real_matching_keeps_past_entry_after_reversal_and_adjacent_first_event_outside_window(self):
        candles = candles_ending(self.target)
        rsis = [50] * len(candles)
        ks = [50] * len(candles)
        ds = [50] * len(candles)
        # RSI at index23 is outside the window; its adjacent stochastic event
        # at24 completes the entry inside the window. Later RSI falls again.
        rsis[22:26] = [19, 21, 22, 19]
        ks[23:25], ds[23:25] = [10, 23], [14, 21]
        # A second entry on the same coin has both events on index27.
        rsis[26:29] = [19, 21, 18]
        ks[26:28], ds[26:28] = [10, 23], [14, 21]
        with patch("indicators.rsi", side_effect=lambda closes: rsis[:len(closes)]), \
                patch("indicators.stochastic", side_effect=lambda prefix: (ks[:len(prefix)], ds[:len(prefix)])):
            result = historical_signals(candles, self.server_time)
        self.assertEqual([signal.close_time for signal in result], [candles[24].close_time, candles[27].close_time])
        self.assertEqual(result[0].rsi_event_close_time, candles[23].close_time)
        self.assertEqual(result[0].stoch_event_close_time, candles[24].close_time)
        self.assertGreater(result[0].rsi_current, 20)
        self.assertLess(rsis[25], 20)

    @patch("history.historical_signals")
    @patch("alerts.AlertStore", side_effect=AssertionError("History must not use the alert database"))
    def test_reports_all_past_entries_without_subscription_or_database_side_effects(self, store, detector):
        detector.return_value = [signal_at(self.target - FOUR_HOURS_MS), signal_at(self.target)]
        result = self.service.scan_once()
        self.assertTrue(result.complete)
        self.assertEqual((result.symbols_checked, result.symbols_total), (1, 1))
        self.assertEqual(len(result.matches), 2)
        self.sender.assert_not_called()
        store.assert_not_called()
        self.assertIn("إشارات تاريخية، وليست تنبيهات جديدة", format_history(result)[0])

    @patch("history.historical_signals", return_value=[])
    def test_partial_failure_never_reports_false_no_signals_or_populates_complete_cache(self, detector):
        self.client.spot_symbols.return_value = ["AAAUSDT", "BBBUSDT"]
        self.client.candles.side_effect = [candles_ending(self.target), MarketError("missing data")]
        result = self.service.scan_once()
        self.assertFalse(result.complete)
        self.assertEqual((result.symbols_checked, result.symbols_total), (1, 2))
        text = format_history(result)[0]
        self.assertIn("الفحص غير مكتمل", text)
        self.assertIn("تعذر التأكد", text)
        self.assertNotIn("لم تظهر إشارات", text)
        self.client.candles.side_effect = lambda *args: candles_ending(self.target)
        self.assertTrue(self.service.scan_once().complete)
        self.assertEqual(self.client.candles.call_count, 4)

    @patch("history.historical_signals", return_value=[])
    def test_market_rate_limit_stops_requests_and_honors_long_future_job_cooldown(self, detector):
        self.client.spot_symbols.return_value = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        self.client.candles.side_effect = [candles_ending(self.target),
                                          MarketError("rate limited", status=429, retry_after=10000)]
        deadline = self.now + 10000
        result = self.service.scan_once()
        self.assertFalse(result.complete)
        self.assertEqual(result.blocked_until, deadline)
        self.assertEqual(self.client.candles.call_count, 2)
        self.now = deadline - 1
        self.assertFalse(self.service.scan_once().complete)
        self.assertEqual(self.client.server_time.call_count, 1)
        self.now = deadline
        self.client.candles.side_effect = lambda *args: candles_ending(self.target)
        self.assertTrue(self.service.scan_once().complete)
        self.assertEqual(self.client.candles.call_count, 5)

    @patch("history.historical_signals", return_value=[])
    def test_cache_reused_with_rolling_cutoff_and_invalidated_after_close_grace(self, detector):
        oldest = self.server_time - DAY_MS + 1
        detector.return_value = [signal_at(oldest), signal_at(self.target)]
        first = self.service.scan_once()
        self.assertEqual(len(first.matches), 2)
        self.server_time += 2
        cached = self.service.scan_once()
        self.assertEqual(len(cached.matches), 1)
        self.assertEqual(self.client.candles.call_count, 1)
        next_boundary = self.target + FOUR_HOURS_MS + 1
        self.server_time = next_boundary + 5_000
        self.service.scan_once()
        self.assertEqual(self.client.candles.call_count, 1)
        self.server_time = next_boundary + CLOSE_GRACE_MS
        self.target += FOUR_HOURS_MS
        self.service.scan_once()
        self.assertEqual(self.client.candles.call_count, 2)
        self.assertEqual(self.client.candles.call_args.args[0], "BTCUSDT")

    def test_chunking_includes_every_entry_newest_first_with_repeated_coin_entries(self):
        matches = tuple((f"COIN{index:04d}USDT", signal_at(self.target - (index % 6) * FOUR_HOURS_MS))
                        for index in range(511)) + (("COIN0000USDT", signal_at(self.target - FOUR_HOURS_MS)),)
        result = HistoryResult(self.server_time, self.target, 511, 511, matches, True, {})
        pages = format_history(result)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page.encode("utf-16-le")) // 2 <= 3500 for page in pages))
        self.assertTrue(all("إشارات تاريخية، وليست تنبيهات جديدة" in page for page in pages))
        listed = Counter()
        for page in pages:
            self.assertIn("<pre>", page)
            for line in visible_report(page).splitlines():
                cells = [cell.strip() for cell in line.split("│")[1:-1]]
                if len(cells) == 6 and cells[0].isdigit():
                    listed[cells[1]] += 1
        self.assertEqual(listed, Counter(symbol for symbol, signal in matches))
        self.assertIn("عدد الإشارات: 512", pages[0])
        self.assertIn("COIN0000USDT", pages[0])

    def test_table_uses_israel_summer_and_winter_time(self):
        for month, expected in ((1, "(14:00)"), (7, "(15:00)")):
            with self.subTest(month=month):
                close = int(datetime(2026, month, 15, 12, tzinfo=timezone.utc).timestamp() * 1000) - 1
                result = HistoryResult(close + 60001, close, 1, 1,
                                       (("BTCUSDT", signal_at(close)),), True, {})
                page = format_history(result)[0]
                text = visible_report(page)
                self.assertNotIn("بتوقيت", text)
                self.assertNotIn("الرياض", text)
                self.assertIn(f"2026-{month:02d}-15", text)
                self.assertIn(expected, text)
                self.assertIn("21.00", text)
                self.assertIn("┌", text)
                self.assertIn("└", text)

    def test_html_special_characters_remain_plain_text(self):
        result = HistoryResult(self.server_time, self.target, 1, 2,
                               (("A<B&USDT", signal_at(self.target)),), False,
                               {"<failed&>": 1})
        page = format_history(result)[0]
        parser = ReportText()
        parser.feed(page)
        self.assertEqual(set(parser.tags), {"b", "pre"})
        self.assertIn("A<B&USDT", "".join(parser.parts))
        self.assertIn("<failed&>", "".join(parser.parts))
        self.assertNotIn("A<B&USDT", page)

    def test_date_groups_repeat_on_every_page_without_losing_wide_symbols(self):
        newer = int(datetime(2026, 1, 15, 0, tzinfo=timezone.utc).timestamp() * 1000) - 1
        older = newer - FOUR_HOURS_MS
        matches = tuple((f"币安人生{index:03d}USDT", signal_at(newer if index % 2 else older))
                        for index in range(100))
        result = HistoryResult(newer + 60001, newer, 100, 100, matches, True, {})
        pages = format_history(result)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all("📅 2026-01-" in visible_report(page) for page in pages))
        text = "\n".join(visible_report(page) for page in pages)
        for symbol, signal in matches:
            self.assertEqual(text.count(symbol), 1)
        self.assertIn("2026-01-15", text)
        self.assertIn("2026-01-14", text)

    @patch("history.historical_signals", return_value=[])
    def test_requests_are_nonblocking_coalesced_and_repeat_uses_cache(self, detector):
        entered = threading.Event()
        release = threading.Event()

        def blocking_candles(*args):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return candles_ending(self.target)

        self.client.candles.side_effect = blocking_candles
        before = time.monotonic()
        self.assertTrue(self.service.request(1))
        self.assertLess(time.monotonic() - before, 0.2)
        self.assertTrue(entered.wait(timeout=1))
        self.assertFalse(self.service.request(1))
        self.assertTrue(self.service.request(2))
        release.set()
        self.wait_for(lambda: self.sender.call_count == 2)
        self.assertEqual(self.client.candles.call_count, 1)
        self.assertEqual({call.args[0] for call in self.sender.call_args_list}, {1, 2})
        self.wait_for(lambda: not self.service._active)
        self.assertTrue(self.service.request(1))
        self.wait_for(lambda: self.sender.call_count == 3)
        self.assertEqual(self.client.candles.call_count, 1)
        self.assertTrue(self.service.is_alive())
        self.shutdown()
        self.assertFalse(self.service.is_alive())
        self.assertFalse(self.service.request(3))

    def test_telegram_pages_paced_and_429_retries_same_page_after_full_cooldown(self):
        attempts = []

        def send(chat_id, page):
            attempts.append((self.now, page))
            if len(attempts) == 1:
                raise AlertDeliveryError("rate limited", status=429, retry_after=10000)

        self.service.send_result = send
        self.assertTrue(self.service._send_pages(1, ["page1", "page2"]))
        self.assertEqual([page for when, page in attempts], ["page1", "page1", "page2"])
        self.assertGreaterEqual(attempts[1][0] - attempts[0][0], 10000)
        self.assertGreaterEqual(attempts[2][0] - attempts[1][0] + 1e-9, 1.05)

    def test_stop_interrupts_delivery_cooldown_and_unexpected_errors_propagate(self):
        self.service._delivery_blocked_until = self.now + 10000

        def stop_sleep(seconds):
            self.stop.set()

        self.service.sleep = stop_sleep
        self.assertFalse(self.service._send_pages(1, ["page1"]))
        self.sender.assert_not_called()
        self.stop.clear()
        self.service._delivery_blocked_until = 0
        self.service.send_result = Mock(side_effect=TypeError("programming bug"))
        with self.assertRaises(TypeError):
            self.service._send_pages(1, ["page1"])

    def test_unexpected_scan_failure_reports_safely_and_preserves_other_queued_request(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        result = HistoryResult(self.server_time, self.target, 1, 1, (), True, {})

        def scan():
            calls.append(True)
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=2)
                raise ValueError("sensitive request detail")
            return result

        self.service.scan_once = Mock(side_effect=scan)
        with self.assertLogs("history", level="ERROR") as logs:
            self.assertTrue(self.service.request(1))
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(self.service.request(2))
            release.set()
            self.wait_for(lambda: self.sender.call_count == 2 and not self.service._active and self.service.last_error is None)
        self.assertEqual(len(calls), 2)
        first, second = self.sender.call_args_list
        self.assertEqual(first.args[0], 1)
        self.assertIn("تعذر إكمال تقرير الإشارات", first.args[1])
        self.assertEqual(second.args[0], 2)
        self.assertIn("إشارات تاريخية، وليست تنبيهات جديدة", second.args[1])
        self.assertIn("ValueError", logs.output[0])
        self.assertNotIn("sensitive request detail", "\n".join(logs.output))
        self.assertTrue(self.service.is_alive())
        self.assertIsNotNone(self.service.last_error_ms)
        self.assertTrue(self.service.request(1))
        self.wait_for(lambda: self.sender.call_count == 3 and not self.service._active)
        self.assertEqual(len(calls), 3)

    def test_unexpected_delivery_failure_keeps_history_worker_available(self):
        result = HistoryResult(self.server_time, self.target, 1, 1, (), True, {})
        self.service.scan_once = Mock(return_value=result)
        self.sender.side_effect = [TypeError("sensitive delivery detail"), None, None]
        with self.assertLogs("history", level="ERROR") as logs:
            self.assertTrue(self.service.request(1))
            self.wait_for(lambda: self.sender.call_count == 2 and not self.service._active)
        self.assertTrue(self.service.is_alive())
        self.assertIn("تعذر إكمال تقرير الإشارات", self.service.last_error)
        self.assertIn("تعذر إكمال تقرير الإشارات", self.sender.call_args_list[1].args[1])
        self.assertNotIn("sensitive delivery detail", "\n".join(logs.output))
        self.assertTrue(self.service.request(2))
        self.wait_for(lambda: self.sender.call_count == 3 and not self.service._active and self.service.last_error is None)

    def test_shared_market_cooldown_deadline_is_not_extended(self):
        error = MarketError("shared cooldown", status=429, retry_after=5)
        error.cooldown_until = self.now + 5
        self.client.server_time.side_effect = error
        result = self.service.scan_once()
        self.assertEqual(result.blocked_until, self.now + 5)
        self.now += 4
        self.assertFalse(self.service.scan_once().complete)
        self.assertEqual(self.client.server_time.call_count, 1)
        self.now += 1
        self.client.server_time.side_effect = lambda: self.server_time
        self.assertTrue(self.service.scan_once().complete)

    @patch("history.format_history", return_value=["page1", "page2"])
    @patch("history.historical_signals", return_value=[])
    def test_partly_delivered_report_exposes_warning_until_later_complete_report(self, detector, formatter):
        self.sender.side_effect = [None, AlertDeliveryError("delivery refused", status=403), None, None]
        with self.assertLogs("history", level="WARNING") as messages:
            self.assertTrue(self.service.request(1))
            self.wait_for(lambda: self.sender.call_count == 2 and not self.service._active)
        self.assertEqual([call.args[1] for call in self.sender.call_args_list], ["page1", "page2"])
        self.assertIn("تقرير الإشارات كاملاً", self.service.last_error)
        self.assertNotIn("delivery refused", self.service.last_error)
        self.assertIn("AlertDeliveryError", messages.output[0])
        self.assertTrue(self.service.request(1))
        self.wait_for(lambda: self.sender.call_count == 4 and not self.service._active and self.service.last_error is None)
        self.assertIsNone(self.service.last_error)
        self.assertEqual(self.client.candles.call_count, 1)


if __name__ == "__main__":
    unittest.main()
