import unittest
from dataclasses import dataclass, replace
from unittest.mock import patch

import indicators


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float


def candles(closes, high=10.0, low=0.0):
    return [Candle(index * 1000, index * 1000 + 999, close, high, low, close)
            for index, close in enumerate(closes)]


class IndicatorTests(unittest.TestCase):
    def test_wilder_rsi_reference_prices(self):
        # Wilder's published example: initial average gain/loss, then recursive smoothing.
        prices = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
                  46.03, 46.41, 46.22, 45.64, 46.21]
        expected = [70.46413502109705, 66.24961855355505, 66.48094183471265,
                    69.34685316290866, 66.29471265892624, 57.91502067008556,
                    62.880718309962404]
        result = indicators.rsi(prices)
        self.assertEqual(result[:14], [None] * 14)
        for actual, reference in zip(result[14:], expected):
            self.assertAlmostEqual(actual, reference, places=10)

    def test_rsi_extremes_and_flat_history(self):
        self.assertEqual(indicators.rsi(range(15))[-1], 100.0)
        self.assertEqual(indicators.rsi(range(15, 0, -1))[-1], 0.0)
        self.assertEqual(indicators.rsi([5] * 15)[-1], 50.0)
        self.assertEqual(indicators.rsi([5] * 14), [None] * 14)

    def test_invalid_rsi_data_resets_warmup(self):
        result = indicators.rsi([1, 2, 3, float("nan"), 4, 5, 6], period=2)
        self.assertEqual(result, [None, None, 100.0, None, None, None, 100.0])

    def test_stochastic_smoothing_matches_manual_calculation(self):
        k, d = indicators.stochastic(candles([2, 4, 6, 8, 3, 1]),
                                    period=3, smooth_k=2, smooth_d=2)
        self.assertEqual(k, [None, None, None, 70.0, 55.0, 20.0])
        self.assertEqual(d, [None, None, None, None, 62.5, 37.5])

    def test_default_stochastic_warmup(self):
        k, d = indicators.stochastic(candles([5] * 18))
        self.assertEqual(k[:15], [None] * 15)
        self.assertEqual(k[15:], [50.0] * 3)
        self.assertEqual(d[:17], [None] * 17)
        self.assertEqual(d[17:], [50.0])

    def test_undefined_and_invalid_stochastic_data(self):
        k, d = indicators.stochastic(candles([5] * 20, high=5, low=5))
        self.assertEqual(k, [None] * 20)
        self.assertEqual(d, [None] * 20)
        corrupt = candles([5] * 20)
        corrupt[-1] = replace(corrupt[-1], high=float("inf"))
        k, d = indicators.stochastic(corrupt)
        self.assertIsNone(k[-1])
        self.assertIsNone(d[-1])

    def test_invalid_periods_are_rejected(self):
        for period in (0, -1, 1.5, True):
            with self.subTest(period=period):
                with self.assertRaises(ValueError):
                    indicators.rsi([1, 2], period=period)
                with self.assertRaises(ValueError):
                    indicators.stochastic([], smooth_d=period)


class EventMatchingTests(unittest.TestCase):
    def test_same_candle_and_rsi_exact_previous_threshold(self):
        self.assertEqual(indicators.match_events([30, 31], [10, 19], [15, 18]), (1, 1))

    def test_stochastic_can_cross_while_still_below_twenty(self):
        self.assertEqual(indicators.match_events([29, 31], [10, 18], [15, 17]), (1, 1))

    def test_rsi_then_stochastic_on_next_candle(self):
        self.assertEqual(indicators.match_events([29, 31, 32], [8, 10, 19], [9, 15, 18]),
                         (1, 2))

    def test_stochastic_then_rsi_on_next_candle(self):
        self.assertEqual(indicators.match_events([28, 29, 31], [10, 19, 27], [15, 18, 26]),
                         (2, 1))

    def test_earlier_rsi_condition_must_persist_at_final_close(self):
        for current_rsi in (29, 30):
            with self.subTest(current_rsi=current_rsi):
                self.assertIsNone(indicators.match_events([29, 31, current_rsi],
                                                          [8, 10, 19], [9, 15, 18]))

    def test_earlier_stochastic_condition_must_persist_at_final_close(self):
        for current_k in (17, 18):
            with self.subTest(current_k=current_k):
                self.assertIsNone(indicators.match_events([28, 29, 31],
                                                          [10, 19, current_k], [15, 18, 18]))

    def test_stochastic_event_boundaries_need_not_persist_next_candle(self):
        # Only K > D must persist; the second candle may exceed 20 or slope down.
        self.assertEqual(indicators.match_events([28, 29, 31],
                                                [10, 19, 40], [15, 18, 35]), (2, 1))
        self.assertEqual(indicators.match_events([28, 29, 31],
                                                [10, 19, 17], [15, 18, 16]), (2, 1))

    def test_stale_signal_and_events_separated_by_two_candles_are_rejected(self):
        self.assertIsNone(indicators.match_events([29, 31, 32], [10, 19, 27], [15, 18, 26]))
        self.assertIsNone(indicators.match_events([29, 31, 32, 33],
                                                  [8, 9, 10, 19], [9, 10, 15, 18]))
        self.assertIsNone(indicators.match_events([27, 28, 29, 31],
                                                  [10, 19, 22, 24], [15, 18, 20, 21]))

    def test_rsi_must_cross_above_thirty(self):
        for rsis in ([29, 30], [31, 32], [31, 29], [None, 31], [float("nan"), 31], [19, 21]):
            with self.subTest(rsis=rsis):
                self.assertIsNone(indicators.match_events(rsis, [10, 19], [15, 18]))

    def test_strict_stochastic_thresholds_and_actual_crossover(self):
        cases = [([20, 25], [19, 24]), ([10, 25], [20, 24]),
                 ([10, 20], [15, 19]), ([10, 21], [15, 20]),
                 ([10, 19], [15, 19]), ([16, 19], [15, 18]),
                 ([None, 19], [15, 18]), ([10, float("inf")], [15, 18]),
                 ([10, 25], [15, 24])]
        for ks, ds in cases:
            with self.subTest(ks=ks, ds=ds):
                self.assertIsNone(indicators.match_events([29, 31], ks, ds))

    def test_values_just_below_stochastic_boundary_are_accepted(self):
        self.assertEqual(indicators.match_events([30, 30.0001], [10, 19.999], [15, 19.998]), (1, 1))

    def test_previous_stochastic_equality_allows_crossover(self):
        self.assertEqual(indicators.match_events([29, 31], [15, 19], [15, 18]), (1, 1))

    def test_crossover_with_falling_or_unchanged_line_is_rejected(self):
        cases = [([10, 19], [15, 14]), ([15, 14], [18, 13]),
                 ([10, 19], [15, 15]), ([15, 15], [18, 14])]
        for ks, ds in cases:
            with self.subTest(ks=ks, ds=ds):
                self.assertIsNone(indicators.match_events([29, 31], ks, ds))

    def test_recent_invalid_value_does_not_use_an_older_event(self):
        self.assertIsNone(indicators.match_events([28, 29, 31],
                                                  [10, 19, None], [15, 18, None]))

    def test_insufficient_and_misaligned_series(self):
        for rsis, ks, ds in (([], [], []), ([31], [19], [18]),
                            ([29, 31], [10], [15, 18])):
            self.assertIsNone(indicators.match_events(rsis, ks, ds))


class EntrySignalTests(unittest.TestCase):
    def test_real_price_rebound_produces_one_fresh_signal(self):
        closes = [100.0 - index for index in range(40)] + [67.0]
        data = [Candle(index * 1000, index * 1000 + 999, close,
                       close + 3, close - 1, close)
                for index, close in enumerate(closes)]
        signal = indicators.entry_signal(data)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.close_time, 40999)
        self.assertEqual(signal.rsi_event_close_time, signal.stoch_event_close_time)
        self.assertAlmostEqual(signal.rsi_current, 100 * 6 / 19)
        self.assertLess(signal.k_previous, 20)
        self.assertLess(signal.d_previous, 20)
        self.assertGreater(signal.k_current, signal.d_current)
        self.assertGreater(signal.k_current, signal.k_previous)
        self.assertGreater(signal.d_current, signal.d_previous)
        self.assertLess(signal.k_current, 20)
        self.assertLess(signal.d_current, 20)
        data.append(Candle(41000, 41999, 68, 71, 67, 68))
        self.assertIsNone(indicators.entry_signal(data))

    def test_signal_values_and_event_timestamps(self):
        data = candles([1, 2, 3])
        with patch("indicators.rsi", return_value=[29, 31, 32]), \
                patch("indicators.stochastic", return_value=([8, 10, 19], [9, 15, 18])):
            signal = indicators.entry_signal(data)
        self.assertEqual(signal, indicators.Signal(
            close_time=2999, rsi_previous=31, rsi_current=32,
            k_previous=10, k_current=19, d_previous=15, d_current=18,
            rsi_event_close_time=1999, stoch_event_close_time=2999))

    def test_incomplete_history_has_no_signal(self):
        self.assertIsNone(indicators.entry_signal([]))
        self.assertIsNone(indicators.entry_signal(candles([1])))
        self.assertIsNone(indicators.entry_signal(candles([1, 2, 3])))

    def test_corrupt_candle_and_wrong_time_order_have_no_signal(self):
        base = candles([1, 2, 3])
        corruptions = [replace(base[-1], close=float("nan")),
                       replace(base[-1], close=11), replace(base[-1], open=11),
                       replace(base[-1], open_time=500, close_time=600),
                       replace(base[-1], open_time=3000, close_time=2999)]
        for corrupted in corruptions:
            with self.subTest(corrupted=corrupted):
                self.assertIsNone(indicators.entry_signal(base[:-1] + [corrupted]))


if __name__ == "__main__":
    unittest.main()
