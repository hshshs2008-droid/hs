from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alerts import AlertDeliveryError, AlertStore, CLOSE_GRACE_MS
from history import HISTORY_WINDOW_MS, HistoryResult, HistoryService, format_history, historical_signals
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
        self.links = []

    def handle_data(self, text):
        self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])


def visible_report(page):
    parser = ReportText()
    parser.feed(page)
    return "".join(parser.parts).replace("\u200e", "")


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

    def durable_store(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = AlertStore(Path(folder.name) / "alerts.sqlite3")
        self.addCleanup(store.close)
        return store

    @patch("history.historical_signals")
    def test_persisted_report_survives_service_and_database_reopen_without_network(self, detector):
        store = self.durable_store()
        store.subscribe(42, 123)
        store.set_update_offset(456)
        self.service.store = store
        detector.return_value = [signal_at(self.target)]
        result = self.service.scan_once()
        self.assertTrue(result.complete)
        self.assertEqual(store.get_update_offset(), 456)
        self.assertEqual(store.subscribers(), [(42, 123)])
        self.assertEqual(store._db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        self.assertEqual(store._db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 0)
        # Use a separate connection to prove that the data is committed to disk.
        reopened = AlertStore(store._db.execute("PRAGMA database_list").fetchone()[2])
        self.addCleanup(reopened.close)
        offline = Mock()
        restored = HistoryService(offline, self.sender, self.stop, request_delay=0,
                                  clock=lambda: self.now, store=reopened)
        self.addCleanup(restored.join, 2)
        cached = restored.cached_result()
        self.assertEqual(cached.matches, result.matches)
        self.assertEqual(cached.cached_at_ms, result.as_of_ms)
        self.assertTrue(restored.request(1))
        self.wait_for(lambda: self.sender.called and not restored._active)
        self.assertIn("BTC/USDT", self.sender.call_args.args[1])
        self.assertIn("آخر تحديث للتقرير", self.sender.call_args.args[1])
        self.assertEqual(offline.mock_calls, [])
        self.stop.set()
        restored.join(2)

    @patch("history.historical_signals", return_value=[])
    def test_background_warms_cache_without_any_report_request(self, detector):
        self.service.store = self.durable_store()
        self.assertTrue(self.service.start_background())
        self.assertFalse(self.service.start_background())
        self.wait_for(lambda: "complete" in self.service.store.get_history_cache())
        self.sender.assert_not_called()
        self.assertEqual(self.client.candles.call_count, 1)
        self.assertTrue(self.service.cached_result().complete)
        self.assertTrue(self.service.is_alive())
        self.shutdown()
        self.assertFalse(self.service.is_alive())

    @patch("history.historical_signals", return_value=[])
    def test_cached_report_is_delivered_while_next_background_scan_is_blocked(self, detector):
        self.service.scan_once()
        self.now += FOUR_HOURS_MS / 1000
        self.server_time += FOUR_HOURS_MS
        self.target += FOUR_HOURS_MS
        entered, release = threading.Event(), threading.Event()

        def blocking_candles(*args):
            entered.set()
            release.wait(timeout=2)
            return candles_ending(self.target)

        self.client.candles.side_effect = blocking_candles
        self.service.start_background()
        try:
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(self.service.request(1))
            self.wait_for(lambda: self.sender.called)
            text = self.sender.call_args.args[1]
            self.assertIn("التحديث في الخلفية", text)
            self.assertNotIn("لم تظهر إشارات مطابقة", text)
            self.assertEqual(self.client.candles.call_count, 2)
        finally:
            release.set()

    @patch("history.historical_signals", return_value=[])
    def test_old_saved_report_does_not_claim_an_empty_current_window_is_complete(self, detector):
        self.service.scan_once()
        self.now += (HISTORY_WINDOW_MS + FOUR_HOURS_MS) / 1000
        snapshot = self.service.cached_result()
        self.assertFalse(snapshot.complete)
        self.assertTrue(snapshot.refreshing)
        self.assertEqual(snapshot.matches, ())
        text = format_history(snapshot)[0]
        self.assertIn("آخر تحديث للتقرير", text)
        self.assertIn("التحديث في الخلفية", text)
        self.assertNotIn("لم تظهر إشارات مطابقة", text)

    def test_invalid_saved_report_is_ignored_and_can_be_replaced(self):
        store = self.durable_store()
        store.save_history_cache("complete", {"version": 2, "window_ms": HISTORY_WINDOW_MS, "result": {"matches": []}})
        with self.assertLogs("history", level="WARNING"):
            service = HistoryService(self.client, self.sender, self.stop, request_delay=0,
                                     clock=lambda: self.now, store=store)
        self.assertIsNone(service.cached_result())
        self.assertTrue(service.scan_once().complete)
        self.assertTrue(service.cached_result().complete)

    @patch("history.historical_signals", return_value=[])
    def test_invalid_saved_json_can_be_replaced_by_background_refresh(self, detector):
        store = self.durable_store()
        with store._db:
            store._db.execute("INSERT INTO metadata(key, value) VALUES ('history_reports', '{broken')")
        self.service.store = store
        self.assertEqual(store.get_history_cache(), {})
        self.assertTrue(self.service.scan_once().complete)
        self.assertTrue(store.get_history_cache()["complete"]["result"]["complete"])

    @patch("history.historical_signals", return_value=[])
    def test_fresh_persisted_report_skips_boot_rescan(self, detector):
        store = self.durable_store()
        self.service.store = store
        self.service.scan_once()
        offline = Mock()
        service = HistoryService(offline, self.sender, self.stop, request_delay=0,
                                 clock=lambda: self.now, store=store)
        # Run a single scheduling iteration without a timer or real network.
        with patch.object(self.stop, "wait", side_effect=lambda delay: self.stop.set()) as wait:
            service._run_background()
        self.assertEqual(offline.mock_calls, [])
        self.assertGreater(wait.call_args.args[0], 0)

    @patch("history.historical_signals", return_value=[])
    def test_new_closed_candle_triggers_background_refresh(self, detector):
        self.service.scan_once()
        self.now += FOUR_HOURS_MS / 1000
        self.server_time += FOUR_HOURS_MS
        self.target += FOUR_HOURS_MS
        with patch.object(self.stop, "wait", side_effect=lambda delay: self.stop.set()):
            self.service._run_background()
        self.assertEqual(self.client.candles.call_count, 2)
        self.assertEqual(self.service._cache.target_close_time, self.target)
        self.assertTrue(self.service.cached_result().complete)

    def test_partial_boot_result_is_saved_but_not_reported_as_complete_after_restart(self):
        store = self.durable_store()
        self.service.store = store
        self.client.server_time.side_effect = MarketError("temporary failure", status=429, retry_after=60)
        self.assertFalse(self.service.scan_once().complete)
        offline = Mock()
        restored = HistoryService(offline, self.sender, self.stop, request_delay=0,
                                  clock=lambda: self.now, store=store)
        snapshot = restored.cached_result()
        self.assertFalse(snapshot.complete)
        self.assertTrue(snapshot.refreshing)
        self.assertNotIn("لم تظهر إشارات مطابقة", format_history(snapshot)[0])
        self.assertEqual(offline.mock_calls, [])

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
    def test_exact_48_hour_boundary_excludes_old_and_future_entries(self, detector):
        as_of = 3 * HISTORY_WINDOW_MS
        cutoff = as_of - HISTORY_WINDOW_MS
        times = [cutoff - 1, cutoff, cutoff + 1, as_of, as_of + 1]
        candles = [Candle(end - 1, end, 10, 11, 9, 10) for end in times]
        detector.side_effect = lambda prefix: signal_at(prefix[-1].close_time)
        result = historical_signals(candles, as_of)
        self.assertEqual([signal.close_time for signal in result], [cutoff + 1, as_of])
        self.assertEqual(detector.call_count, 2)
        self.assertEqual(detector.call_args.args[0][-1].close_time, as_of)

    @patch("history.entry_signal")
    def test_window_includes_36_hour_old_signals_and_excludes_49_hour_old_signals(self, detector):
        hour_ms = 60 * 60 * 1000
        as_of = self.server_time
        times = [as_of - 49 * hour_ms, as_of - 36 * hour_ms, as_of - 4 * hour_ms]
        candles = [Candle(end - 1, end, 10, 11, 9, 10) for end in times]
        detector.side_effect = lambda prefix: signal_at(prefix[-1].close_time)
        matches = historical_signals(candles, as_of)
        self.assertEqual([signal.close_time for signal in matches], times[1:])
        report = HistoryResult(as_of, self.target, 1, 1,
                               tuple(("BTCUSDT", signal) for signal in matches), True, {})
        page = format_history(report)[0]
        self.assertIn("إشارات آخر 48 ساعة", page)
        self.assertIn("عدد الإشارات: 2", page)
        self.service._cache = report
        self.assertEqual(self.service.cached_result().matches, report.matches)

    @patch("history.historical_signals")
    def test_24_hour_saved_reports_are_rebuilt_for_48_hours(self, detector):
        store = self.durable_store()
        self.service.store = store
        detector.return_value = [signal_at(self.target)]
        self.service.scan_once()
        original = store.get_history_cache()["complete"]
        for version in (1, 2):
            with self.subTest(version=version):
                old = dict(original, version=version, window_ms=24 * 60 * 60 * 1000)
                store.save_history_cache("complete", old)
                restored = HistoryService(self.client, self.sender, self.stop, request_delay=0,
                                          clock=lambda: self.now, store=store)
                self.assertIsNone(restored.cached_result())
                detector.return_value = [signal_at(self.target - 9 * FOUR_HOURS_MS), signal_at(self.target)]
                result = restored.scan_once()
                self.assertTrue(result.complete)
                self.assertEqual(len(result.matches), 2)
                self.assertEqual(store.get_history_cache()["complete"]["window_ms"], 48 * 60 * 60 * 1000)

    def test_real_matching_keeps_past_entry_after_reversal_and_adjacent_first_event_outside_window(self):
        candles = candles_ending(self.target, count=36)
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
        oldest = self.server_time - HISTORY_WINDOW_MS + 1
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
        serials = []
        for page in pages:
            self.assertNotIn("<pre>", page)
            for line in visible_report(page).splitlines():
                row = re.fullmatch(r"(\d+)\. (\S+) ✅ (\d{4}-\d{2}-\d{2}) (\(\d{2}:\d{2}\))", line)
                if row:
                    serials.append(int(row[1]))
                    listed[row[2].replace("/", "")] += 1
        self.assertEqual(serials, list(range(1, 513)))
        self.assertEqual(listed, Counter(symbol for symbol, signal in matches))
        self.assertIn("عدد الإشارات: 512", pages[0])
        self.assertIn("COIN0000/USDT", pages[0])

    def test_list_uses_summer_and_winter_time(self):
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
                self.assertNotIn("RSI", text)
                self.assertNotIn("ستوكاست", text)
                self.assertIn(f"01. BTC/USDT ✅ 2026-{month:02d}-15 {expected}", text)
                self.assertIn("<code>BTC/USDT</code>", page)
                self.assertNotIn("<a ", page)
                self.assertNotIn("<pre>", page)
                self.assertNotIn("│", text)

    def test_html_special_characters_remain_plain_text(self):
        result = HistoryResult(self.server_time, self.target, 1, 2,
                               (("A<B&USDT", signal_at(self.target)),), False,
                               {"<failed&>": 1})
        page = format_history(result)[0]
        parser = ReportText()
        parser.feed(page)
        self.assertEqual(set(parser.tags), {"b", "code"})
        self.assertEqual(parser.links, [])
        self.assertIn("A<B&/USDT", "".join(parser.parts))
        self.assertIn("<failed&>", "".join(parser.parts))
        self.assertNotIn("A<B&USDT", page)

    def test_list_keeps_dates_and_all_wide_symbols_on_multiple_pages(self):
        newer = int(datetime(2026, 1, 15, 0, tzinfo=timezone.utc).timestamp() * 1000) - 1
        older = newer - FOUR_HOURS_MS
        matches = tuple((f"币安人生{index:03d}USDT", signal_at(newer if index % 2 else older))
                        for index in range(100))
        result = HistoryResult(newer + 60001, newer, 100, 100, matches, True, {})
        pages = format_history(result)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all("<pre>" not in page for page in pages))
        text = "\n".join(visible_report(page) for page in pages)
        for symbol, signal in matches:
            self.assertEqual(text.count(symbol[:-4] + "/USDT"), 1)
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
