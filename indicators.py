"""Closed-candle RSI and slow Stochastic entry filter, without network access.

The caller must supply completed candles, ordered oldest to newest. RSI uses
Wilder smoothing; slow Stochastic uses SMA(14 raw %K, 3 %K, 3 %D).
"""

from dataclasses import dataclass
from math import isfinite


def _number(value):
    """Return a finite number, or None for malformed market data."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _period(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Indicator periods must be positive integers.")


def rsi(closes, period=14):
    """Aligned Wilder RSI; the first value needs period + 1 valid closes.

    An unchanged price history has RSI 50. Invalid prices reset the warm-up,
    preventing stale smoothing values from bridging missing or corrupt data.
    """
    _period(period)
    values = [_number(close) for close in closes]
    result = [None] * len(values)
    previous = None
    changes = 0
    gain_sum = loss_sum = 0.0
    average_gain = average_loss = None
    for index, close in enumerate(values):
        if close is None:
            previous = None
            changes = 0
            gain_sum = loss_sum = 0.0
            average_gain = average_loss = None
            continue
        if previous is None:
            previous = close
            continue
        difference = close - previous
        gain, loss = max(difference, 0.0), max(-difference, 0.0)
        previous = close
        if average_gain is None:
            gain_sum += gain
            loss_sum += loss
            changes += 1
            if changes < period:
                continue
            average_gain = gain_sum / period
            average_loss = loss_sum / period
        else:
            average_gain = (average_gain * (period - 1) + gain) / period
            average_loss = (average_loss * (period - 1) + loss) / period
        if average_gain == 0.0 and average_loss == 0.0:
            result[index] = 50.0
        elif average_loss == 0.0:
            result[index] = 100.0
        else:
            result[index] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return result


def _sma(values, period):
    result = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        if all(value is not None for value in window):
            result[index] = sum(window) / period
    return result


def _prices(candle):
    try:
        high, low, close = (_number(candle.high), _number(candle.low),
                            _number(candle.close))
    except AttributeError:
        return None
    if high is None or low is None or close is None or not low <= close <= high:
        return None
    return high, low, close


def stochastic(candles, period=14, smooth_k=3, smooth_d=3):
    """Aligned slow %K and %D; undefined zero-range windows stay None."""
    for parameter in (period, smooth_k, smooth_d):
        _period(parameter)
    prices = [_prices(candle) for candle in candles]
    raw = [None] * len(prices)
    for index in range(period - 1, len(prices)):
        window = prices[index - period + 1:index + 1]
        if any(price is None for price in window):
            continue
        high = max(price[0] for price in window)
        low = min(price[1] for price in window)
        if high > low:
            raw[index] = 100.0 * (prices[index][2] - low) / (high - low)
    k = _sma(raw, smooth_k)
    return k, _sma(k, smooth_d)


def match_events(rsis, ks, ds):
    """Return (RSI index, Stochastic index) only for a fresh entry signal.

    RSI crosses from <= 20 to > 20. Stochastic crosses K above D, with both
    lines below 20 on the preceding candle and below 30 on the event candle;
    both lines must rise compared with their preceding values.
    Both events occur on the newest candle or on two consecutive candles,
    in either order; at least one event must be on the newest candle.
    At the newest close RSI must remain above 20 and K must remain above D.
    """
    size = len(rsis)
    if size < 2 or len(ks) != size or len(ds) != size:
        return None
    if any(_number(value) is None for series in (rsis, ks, ds) for value in series[-2:]):
        return None
    if _number(rsis[-1]) <= 20 or _number(ks[-1]) <= _number(ds[-1]):
        return None

    def rsi_event(index):
        if index < 1:
            return False
        previous, current = _number(rsis[index - 1]), _number(rsis[index])
        return previous is not None and current is not None and previous <= 20 < current

    def stoch_event(index):
        if index < 1:
            return False
        previous_k, current_k = _number(ks[index - 1]), _number(ks[index])
        previous_d, current_d = _number(ds[index - 1]), _number(ds[index])
        if any(value is None for value in (previous_k, current_k, previous_d, current_d)):
            return False
        return (previous_k < 20 and previous_d < 20 and previous_k <= previous_d
                and current_k > current_d and current_k < 30 and current_d < 30
                and current_k > previous_k and current_d > previous_d)

    newest = size - 1
    for rsi_index, stoch_index in ((newest, newest), (newest - 1, newest),
                                  (newest, newest - 1)):
        if rsi_event(rsi_index) and stoch_event(stoch_index):
            return rsi_index, stoch_index
    return None


@dataclass(frozen=True)
class Signal:
    close_time: int
    rsi_previous: float
    rsi_current: float
    k_previous: float
    k_current: float
    d_previous: float
    d_current: float
    rsi_event_close_time: int
    stoch_event_close_time: int


def entry_signal(candles):
    """Evaluate completed, chronological candles using the approved filter.

    Event times may refer to the preceding candle. The previous/current
    indicator fields always describe the newest two completed candles.
    """
    candles = list(candles)
    if len(candles) < 2:
        return None
    previous_time = None
    for candle in candles:
        prices = _prices(candle)
        if prices is None:
            return None
        try:
            close_time = candle.close_time
            open_time = candle.open_time
            opening_price = _number(candle.open)
        except AttributeError:
            return None
        if (isinstance(close_time, bool) or not isinstance(close_time, int)
                or isinstance(open_time, bool) or not isinstance(open_time, int)
                or open_time > close_time or opening_price is None
                or not prices[1] <= opening_price <= prices[0]
                or previous_time is not None and open_time <= previous_time):
            return None
        previous_time = close_time
    rsis = rsi([candle.close for candle in candles])
    ks, ds = stochastic(candles)
    events = match_events(rsis, ks, ds)
    if events is None:
        return None
    return Signal(
        close_time=candles[-1].close_time,
        rsi_previous=rsis[-2], rsi_current=rsis[-1],
        k_previous=ks[-2], k_current=ks[-1],
        d_previous=ds[-2], d_current=ds[-1],
        rsi_event_close_time=candles[events[0]].close_time,
        stoch_event_close_time=candles[events[1]].close_time,
    )
