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

    def snapshot(self, source, symbol, sent_ms, now_ms):
        """Read one committed checkpoint; no network or acquisition of scan lock."""
        if type(sent_ms) is not int:
            return {'error':'وقت الإرسال غير متاح'}
        start = ((sent_ms+MINUTE-1)//MINUTE)*MINUTE
        key = 'peak_v1:' + hashlib.sha256(json.dumps([source,symbol,start]).encode()).hexdigest()
        cached = self.store._metadata(key, None)
        if not cached or not start < cached['through'] <= now_ms:
            return {'error':'بانتظار تحديث القمة في الخلفية'}
        result = {'start':start,'through':cached['through']}
        if cached.get('upper') is not None or cached.get('price') is None:
            result['error'] = 'تعذر إثبات القمة القديمة؛ الاستعادة قيد المتابعة'
        else:
            result['price'] = cached['price']
        return result

    def _commit(self, key, value):
        # Reports and the tracker may finish in either order. Never rewind coverage.
        with self.store._lock:
            old = self.store._metadata(key, None)
            if old and old['through'] > value['through']:
                return
            if old and old.get('price') is not None and value.get('price') is not None:
                value['price'] = str(max(Decimal(old['price']), Decimal(value['price'])))
            if old and old.get('observed_high') is not None:
                if (value.get('observed_high') is None
                        or Decimal(old['observed_high']) > Decimal(value['observed_high'])):
                    value['observed_high'] = old['observed_high']
                    value['observed_at'] = old['observed_at']
            self.store._set_metadata(key, value)

    def refresh_report(self, rows, quotes, *, clock=time.time):
        """One bounded minute tail per market; no historical replay on /report.

        Only closed minutes advance the persistent cursor. The last live candle
        and quote are observations for this report, never marked complete.
        """
        from reporting import _key_parts, _quote_key, _price
        groups = {}
        for row in rows:
            source, symbol = _key_parts(_quote_key(row))
            groups.setdefault((source,symbol), []).append(row)
        blocked = set()
        for (source,symbol), members in groups.items():
            if self.stop.is_set(): return
            now = int(clock()*1000)
            closed_end = now//MINUTE*MINUTE
            candidates = []
            for row in members:
                sent = row.get('telegram_sent_at_ms')
                row['peak'] = {'error':'بانتظار استكمال القمة في الخلفية'}
                if type(sent) is not int: continue
                start = (sent+MINUTE-1)//MINUTE*MINUTE
                key = 'peak_v1:'+hashlib.sha256(json.dumps([source,symbol,start]).encode()).hexdigest()
                cached = self.store._metadata(key, None)
                cursor = cached['through'] if cached else start
                if cursor < start or cursor > closed_end: continue
                # At most 500 candles including the live minute (~8h 19m).
                if closed_end-cursor > 499*MINUTE: continue
                candidates.append((row,key,start,cursor,cached or {}))
            if not candidates or source in blocked: continue
            first = min(item[3] for item in candidates)
            try:
                if self.stop.wait(.15): return
                client = self.router.binance_client if source=='binance' else self.router.gate_client
                if client is None: raise MarketError('Original market unavailable.')
                if source=='binance':
                    data = client._get('/api/v3/klines', {'symbol':symbol,'interval':'1m',
                        'startTime':first,'endTime':now,'limit':500})
                else:
                    data = client._get('/spot/candlesticks', {'currency_pair':symbol,'interval':'1m',
                        'from':first//1000,'to':now//1000})
                observed = int(clock()*1000)
                found = {}
                try:
                    if not isinstance(data,list): raise ValueError()
                    for candle in data:
                        stamp = int(candle[0])*(1000 if source=='gate' else 1)
                        if not first <= stamp <= closed_end: continue
                        if source=='gate':
                            from gate_market import _is_closed_flag, _closed_flag
                            flag = candle[7] if len(candle)==8 else candle[6] if len(candle)==7 and _is_closed_flag(candle[6]) else None
                            if stamp < closed_end and flag is not None and not _closed_flag(flag): raise ValueError()
                        elif int(candle[6]) != stamp+MINUTE-1: raise ValueError()
                        high = Decimal(str(candle[3] if source=='gate' else candle[2]))
                        if not high.is_finite() or high<=0 or stamp in found: raise ValueError()
                        found[stamp] = high
                    if set(found) != set(range(first,closed_end+MINUTE,MINUTE)): raise ValueError()
                except (ValueError,TypeError,IndexError,InvalidOperation):
                    raise MarketError('Incomplete report minute tail.') from None
                # Suffix maxima let repeated alerts share the same response.
                suffix = {}; high = None
                for stamp in range(closed_end-MINUTE,first-MINUTE,-MINUTE):
                    high = max(high,found[stamp]) if high is not None else found[stamp]
                    suffix[stamp] = high
                for row,key,start,cursor,cached in candidates:
                    closed_high = Decimal(cached['price']) if cached.get('price') is not None else None
                    if cursor in suffix:
                        closed_high = max(closed_high,suffix[cursor]) if closed_high is not None else suffix[cursor]
                    upper = Decimal(cached['upper']) if cached.get('upper') is not None else None
                    if upper is not None and closed_high is not None and closed_high>=upper: upper=None
                    peak = max(closed_high,found[closed_end]) if closed_high is not None else found[closed_end]
                    if (cached.get('observed_high') is not None
                            and start <= cached.get('observed_at',0) <= observed):
                        peak = max(peak,Decimal(cached['observed_high']))
                    quote = quotes.get(_quote_key(row),{})
                    price = _price(quote.get('price'))
                    at = quote.get('at_ms')
                    if price is not None and type(at) is int and start <= at <= observed:
                        peak = max(peak,price)
                    result = {'start':start,'through':closed_end,
                        'observed_at_ms':min(observed,closed_end+MINUTE),
                        'live_minute':closed_end}
                    if upper is not None and peak<upper:
                        result['error']='تعذر إثبات القمة القديمة؛ حدّ الفترة المفقودة أعلى من السعر المؤكد'
                    else:
                        result['price']=str(peak)
                    self._commit(key, {'price':str(closed_high) if closed_high is not None else None,
                        'through':closed_end,'upper':str(upper) if upper is not None else None,
                        'recovered':cached.get('recovered',False),
                        'observed_high':str(peak),'observed_at':result['observed_at_ms']})
                    row['peak']=result
            except MarketError as error:
                for row, *_ in candidates:
                    row['peak']={'error':'تعذر تحديث الفترة الأخيرة؛ لا تُعرض القمة القديمة كقمة حالية'}
                if error.status in (403,418,429,451): blocked.add(source)
                logger.warning('Report tail unavailable; source=%s symbol=%s status=%s',source,symbol,error.status)

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
        if cached and start <= cached['through']:
            cursor = cached['through']
            high = Decimal(cached['price']) if cached.get('price') is not None else None
            upper = Decimal(cached['upper']) if cached.get('upper') is not None else None
            recovered = cached.get('recovered', False)
            if (cached.get('observed_high') is not None
                    and start <= cached.get('observed_at',0) <= end):
                observed_high = Decimal(cached['observed_high'])
                high = max(high,observed_high) if high is not None else observed_high
                if upper is not None and high >= upper: upper = None
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
            self._commit(key, {'price':str(high) if high is not None else None,
                'through':cursor,'upper':str(upper) if upper is not None else None,
                'recovered':recovered})
        if upper is not None or high is None:
            return {'error':'تعذر إثبات القمة: دقائق قديمة وحدّ الشمعة المتداخلة أعلى من القمة المؤكدة.',
                    'start':start,'through':cursor}
        return {'price':str(high),'start':start,'through':cursor}


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
