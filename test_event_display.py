"""Event display regression tests: indicator decisions and latest values stay intact."""
from dataclasses import asdict
from datetime import datetime
import json
import re
import unittest
from unittest.mock import patch

from alerts import AlertStore, DISPLAY_TZ, _refresh_alert_times, format_alert
from indicators import Signal, STRATEGY_VERSION, entry_signal
from market import Candle, FOUR_HOURS_MS


def candle_series(hour=10):
    end = int(datetime(2026, 11, 1, hour, tzinfo=DISPLAY_TZ).timestamp() * 1000) - 1
    return [Candle(close - FOUR_HOURS_MS + 1, close, 10, 11, 9, 10)
            for close in (end - 2 * FOUR_HOURS_MS, end - FOUR_HOURS_MS, end)]


def evaluate(rsis, ks, ds, hour=10):
    candles = candle_series(hour)
    with patch('indicators.rsi', return_value=rsis), \
            patch('indicators.stochastic', return_value=(ks, ds)):
        signal = entry_signal(candles)
    return signal, candles


class EventDisplayTests(unittest.TestCase):
    def test_same_candle_event_fields_match_latest_fields(self):
        signal, candles = evaluate([28, 29, 35], [8, 9, 15], [10, 11, 13])
        self.assertIsNotNone(signal)
        self.assertEqual(signal.close_time, candles[-1].close_time)
        self.assertEqual(signal.rsi_event_close_time, signal.close_time)
        self.assertEqual(signal.stoch_event_close_time, signal.close_time)
        for prefix in ('rsi', 'k', 'd'):
            for side in ('previous', 'current'):
                self.assertEqual(getattr(signal, f'{prefix}_event_{side}'),
                                 getattr(signal, f'{prefix}_{side}'))
        message = format_alert('BTCUSDT', signal)
        self.assertEqual(message.splitlines()[-1], 'الشرطان في الشمعة نفسها. 🔗')
        self.assertIn('🕒 4H إغلاق الشمعة: 2026-11-01 (10:00)', message)
        self.assertIn('↗️ RSI عبور فوق الـ30: (10:00)', message)
        self.assertIn('🔀 K فوق D تقاطع في (10:00)', message)

    def test_rsi_first_shows_crossing_instead_of_flat_latest_values(self):
        signal, candles = evaluate([28.85, 34.67, 34.67], [7.82, 9.95, 17.98], [14.07, 10.40, 11.91])
        self.assertEqual((signal.rsi_previous, signal.rsi_current), (34.67, 34.67))
        self.assertEqual((signal.rsi_event_previous, signal.rsi_event_current), (28.85, 34.67))
        self.assertEqual(signal.rsi_event_close_time, candles[-2].close_time)
        self.assertEqual(signal.stoch_event_close_time, candles[-1].close_time)
        message = format_alert('DKA_USDT', signal, exchange_name='Gate.io Spot')
        self.assertIn('📊 RSI (14): 28.85 → 34.67', message)
        self.assertIn('🔹 K: 9.95 → 17.98', message)
        self.assertIn('🔸 D: 10.40 → 11.91', message)
        self.assertNotIn('RSI (14): 34.67 → 34.67', message)
        self.assertIn('↗️ RSI عبور فوق الـ30: (06:00)', message)
        self.assertIn('🔀 K فوق D تقاطع في (10:00)', message)
        self.assertEqual(re.findall(r'\d{4}-\d{2}-\d{2}', message), ['2026-11-01'])
        self.assertIn('🕒 4H إغلاق الشمعة: 2026-11-01 (10:00)', message)
        self.assertEqual(message.splitlines()[-1], 'الشرطان في شمعتين متتاليتين. 🔗')

    def test_stochastic_first_shows_under_twenty_crossing(self):
        signal, candles = evaluate([28, 29, 35], [8, 15, 26], [10, 13, 18])
        self.assertEqual((signal.k_previous, signal.k_current), (15, 26))
        self.assertEqual((signal.d_previous, signal.d_current), (13, 18))
        self.assertEqual((signal.k_event_previous, signal.k_event_current), (8, 15))
        self.assertEqual((signal.d_event_previous, signal.d_event_current), (10, 13))
        self.assertEqual(signal.rsi_event_close_time, candles[-1].close_time)
        self.assertEqual(signal.stoch_event_close_time, candles[-2].close_time)
        message = format_alert('BTCUSDT', signal)
        self.assertIn('📊 RSI (14): 29.00 → 35.00', message)
        self.assertIn('🔹 K: 8.00 → 15.00', message)
        self.assertIn('🔸 D: 10.00 → 13.00', message)
        self.assertIn('↗️ RSI عبور فوق الـ30: (10:00)', message)
        self.assertIn('🔀 K فوق D تقاطع في (06:00)', message)

    def test_previous_day_event_is_identified_without_repeating_date(self):
        signal, _ = evaluate([28.85, 34.67, 34.67], [7.82, 9.95, 17.98], [14.07, 10.40, 11.91], hour=2)
        message = format_alert('DKA_USDT', signal, exchange_name='Gate.io Spot')
        self.assertIn('🕒 4H إغلاق الشمعة: 2026-11-01 (02:00)', message)
        event_line = next(line for line in message.splitlines() if 'RSI عبور' in line)
        self.assertIn('(22:00)', event_line)
        self.assertIn('اليوم السابق', event_line)
        self.assertEqual(re.findall(r'\d{4}-\d{2}-\d{2}', message), ['2026-11-01'])

    def test_pending_refresh_preserves_event_values_and_is_idempotent(self):
        signal, _ = evaluate([28.85, 34.67, 34.67], [7.82, 9.95, 17.98], [14.07, 10.40, 11.91])
        store = AlertStore(':memory:', exchange_name='Gate.io Spot')
        self.addCleanup(store.close)
        store.subscribe(42, signal.close_time - 1)
        store.record('DKA_USDT', signal.close_time, signal)
        pending = store.pending()
        self.assertEqual(len(pending), 1)
        message = pending[0][3]
        self.assertIn('RSI (14): 28.85 → 34.67', message)
        refreshed = _refresh_alert_times(message, signal.close_time,
                                         signal.rsi_event_close_time, signal.stoch_event_close_time)
        self.assertEqual(message, refreshed)
        self.assertEqual(store.pending()[0][3], message)
        legacy_icon = message.replace('الشرطان في شمعتين متتاليتين. 🔗',
                                      '🔗 الشرطان في شمعتين متتاليتين.')
        self.assertEqual(_refresh_alert_times(legacy_icon, signal.close_time,
                                             signal.rsi_event_close_time, signal.stoch_event_close_time),
                         message)
        self.assertEqual(message.count('🔗'), 1)

    def test_legacy_signal_without_event_values_still_deserializes(self):
        old = dict(close_time=1000, rsi_previous=29, rsi_current=31,
                   k_previous=10, k_current=18, d_previous=14, d_current=16,
                   rsi_event_close_time=1000, stoch_event_close_time=1000)
        signal = Signal(**json.loads(json.dumps(old)))
        self.assertIsNone(signal.rsi_event_previous)
        self.assertIsNone(signal.k_event_current)
        self.assertIn('RSI (14): 29.00 → 31.00', format_alert('BTCUSDT', signal))
        self.assertEqual(Signal(**json.loads(json.dumps(asdict(signal)))), signal)
        self.assertEqual(signal.rsi_threshold, 30)

    def test_invalid_strategy_conditions_remain_rejected(self):
        # Merely furnishing event display data must not relax entry thresholds.
        for rsis, ks, ds in (([28, 29, 30], [8, 9, 15], [10, 11, 13]),
                            ([28, 29, 35], [8, 9, 21], [10, 11, 13]),
                            ([28, 29, 35], [8, 9, 15], [10, 14, 13])):
            with self.subTest(rsis=rsis, ks=ks, ds=ds):
                signal, _ = evaluate(rsis, ks, ds)
                self.assertIsNone(signal)


if __name__ == '__main__':
    unittest.main()
