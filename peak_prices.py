"""Compact post-send maxima. Coarse boundary candles are bounds, never entries."""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import threading
import time
from market import MarketError, HistoricalWindowError

MINUTE = 60000
HOUR = 3600000
STEPS = (MINUTE, 5*MINUTE, 15*MINUTE, 30*MINUTE, HOUR, 4*HOUR, 24*HOUR)
INTERVALS = dict(zip(STEPS, ('1m','5m','15m','30m','1h','4h','1d')))
logger = logging.getLogger(__name__)


class PeakPrices:
    def __init__(self, router, store, stop):
        self.router, self.store, self.stop = router, store, stop
        self._lock = threading.RLock()
        # Only two extrema per segment, bounded RAM; never persist candle history.
        self._segments = OrderedDict()

    def _segment(self, source, symbol, start, finish, step):
        key = (source, symbol, start, finish, step)
        if key in self._segments:
            self._segments.move_to_end(key)
            return self._segments[key]
        if self.stop.wait(.15):
            raise MarketError('Peak scan stopped.')
        client = self.router.binance_client if source == 'binance' else self.router.gate_client
        if client is None:
            raise MarketError('Original market unavailable.')
        interval = INTERVALS[step]
        if source == 'binance':
            rows = client._get('/api/v3/klines', {'symbol':symbol,'interval':interval,
                'startTime':start,'endTime':finish-1,'limit':500})
        else:
            rows = client._get('/spot/candlesticks', {'currency_pair':symbol,'interval':interval,
                'from':start//1000,'to':(finish-1)//1000})
        found = {}
        try:
            if not isinstance(rows, list): raise ValueError()
            for row in rows:
                stamp = int(row[0]) * (1000 if source == 'gate' else 1)
                if not start <= stamp < finish: continue
                if source == 'gate':
                    from gate_market import _is_closed_flag, _closed_flag
                    flag = row[7] if len(row) == 8 else row[6] if len(row) == 7 and _is_closed_flag(row[6]) else None
                    if flag is not None and not _closed_flag(flag): continue
                peak = Decimal(str(row[3] if source == 'gate' else row[2]))
                if not peak.is_finite() or peak <= 0 or stamp in found: raise ValueError()
                if source == 'binance' and int(row[6]) != stamp+step-1: raise ValueError()
                found[stamp] = peak
            if set(found) != set(range(start,finish,step)): raise ValueError()
        except (ValueError,TypeError,IndexError,InvalidOperation):
            raise MarketError('Incomplete historical high coverage.') from None
        first = found.pop(start)
        value = (first, max(found.values()) if found else None)
        self._segments[key] = value
        if len(self._segments) > 512: self._segments.popitem(last=False)
        return value

    def measure(self, source, symbol, sent_ms, now_ms):
        with self._lock:
            return self._measure(source, symbol, sent_ms, now_ms)

    def _measure(self, source, symbol, sent_ms, now_ms):
        if source not in ('gate', 'binance'):
            raise ValueError('Unknown peak source')
        if type(sent_ms) is not int:
            return {'error': 'وقت الإرسال غير متاح'}
        start = ((sent_ms + MINUTE-1)//MINUTE)*MINUTE
        end = (now_ms//MINUTE)*MINUTE
        if end <= start:
            return {'error': 'لم تكتمل دقيقة بعد الإرسال'}
        key = 'peak_v1:' + hashlib.sha256(json.dumps([source,symbol,start]).encode()).hexdigest()
        cached = self.store._metadata(key, None)
        cursor, high, upper = start, None, None
        recovered = False
        if cached and start <= cached['through'] <= end:
            cursor = cached['through']
            high = Decimal(cached['price']) if cached.get('price') is not None else None
            upper = Decimal(cached['upper']) if cached.get('upper') is not None else None
            recovered = cached.get('recovered', False)
        while cursor < end:
            if self.stop.wait(0): raise MarketError('Peak scan stopped.')
            step = HOUR if cursor % HOUR == 0 and end-cursor >= HOUR else MINUTE
            while True:
                aligned = cursor//step*step
                boundary = end if step >= HOUR else min(end, (cursor//HOUR+1)*HOUR)
                count = min(500, (boundary-aligned)//step)
                if count < 1:
                    raise MarketError('لا تتوفر بيانات دقيقة كافية لتحديد القمة القديمة.')
                finish = aligned+count*step
                try:
                    first, rest = self._segment(source, symbol, aligned, finish, step)
                    break
                except HistoricalWindowError:
                    if source != 'gate' or step == STEPS[-1]: raise
                    step = STEPS[STEPS.index(step)+1]
                    recovered = True
            # The first coarse candle may include prices before cursor/sending.
            # Its high is only an upper bound on the missing portion.
            if aligned < cursor:
                upper = max(upper, first) if upper is not None else first
                candidate = rest
            else:
                candidate = max(first, rest) if rest is not None else first
            if candidate is not None:
                high = max(high, candidate) if high is not None else candidate
            if high is not None and upper is not None and high >= upper:
                upper = None  # Proved: no unknown boundary price can exceed high.
            cursor = finish
            self.store._set_metadata(key, {'price':str(high) if high is not None else None,
                'through':cursor,'upper':str(upper) if upper is not None else None,
                'recovered':recovered})
        if upper is not None or high is None:
            return {'error':'تعذر إثبات القمة: دقائق قديمة وحدّ الشمعة المتداخلة أعلى من القمة المؤكدة.',
                    'start':start,'through':end}
        return {'price':str(high),'start':start,'through':end}


class PeakTracker(threading.Thread):
    """Four catch-ups/day, using closed hours and the shared report ledger/cache."""
    def __init__(self, provider, rows, stop, *, clock=time.time):
        super().__init__(name='peak-tracker', daemon=True)
        self.provider, self.rows, self.stop, self.clock = provider, rows, stop, clock

    def run_once(self):
        from reporting import _key_parts, _quote_key
        end = int(self.clock()*1000)//HOUR*HOUR
        blocked = set()
        checked = complete = 0
        # Prioritize recent alerts so recovery of old ones cannot starve new starts.
        rows = sorted(self.rows(), key=lambda r:r.get('telegram_sent_at_ms') or 0, reverse=True)
        for row in rows:
            if self.stop.is_set(): break
            source, symbol = _key_parts(_quote_key(row))
            if source in blocked: continue
            checked += 1
            try:
                result = self.provider.measure(source, symbol, row.get('telegram_sent_at_ms'), end)
                complete += int('price' in result)
            except MarketError as exc:
                if exc.status in (403,418,429,451): blocked.add(source)
                logger.warning('Peak tracker incomplete; source=%s symbol=%s status=%s',source,symbol,exc.status)

        logger.info('Peak tracker checked=%s complete=%s incomplete=%s through_ms=%s', checked, complete, checked-complete, end)

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                logger.error('Peak tracker retry; exception type=%s',type(exc).__name__)
            if self.stop.wait(6*3600): break
