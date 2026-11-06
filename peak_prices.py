"""Conservative post-send highs over complete minutes; persistent shared cache."""
from decimal import Decimal, InvalidOperation
import hashlib
import json
from market import MarketError

MINUTE = 60000
HOUR = 3600000


class PeakPrices:
    def __init__(self, router, store, stop):
        self.router, self.store, self.stop = router, store, stop

    def measure(self, source, symbol, sent_ms, now_ms):
        if type(sent_ms) is not int:
            return {'error': 'وقت الإرسال غير متاح'}
        start = ((sent_ms + MINUTE-1)//MINUTE)*MINUTE
        end = (now_ms//MINUTE)*MINUTE
        if end <= start:
            return {'error': 'لم تكتمل دقيقة بعد الإرسال'}
        key = 'peak_v1:' + hashlib.sha256(json.dumps([source,symbol,start]).encode()).hexdigest()
        cached = self.store._metadata(key, None)
        cursor = start
        high = None
        if cached and start <= cached['through'] <= end:
            cursor, high = cached['through'], Decimal(cached['price'])
        while cursor < end:
            if self.stop.wait(.15):
                raise MarketError('Peak scan stopped.')
            step = HOUR if cursor % HOUR == 0 and end-cursor >= HOUR else MINUTE
            boundary = end if step == HOUR else min(end, ((cursor//HOUR)+1)*HOUR)
            count = min(500, (boundary-cursor)//step)
            finish = cursor+count*step
            segment = f'peak_segment_v1:{source}:{symbol}:{cursor}:{finish}:{step}'
            value = self.store._metadata(segment, None)
            if value is None:
                client = self.router.binance_client if source == 'binance' else self.router.gate_client
                if client is None:
                    raise MarketError('Original market unavailable.')
                interval = '1h' if step == HOUR else '1m'
                if source == 'binance':
                    rows = client._get('/api/v3/klines', {'symbol':symbol,'interval':interval,
                        'startTime':cursor,'endTime':finish-1,'limit':500})
                else:
                    rows = client._get('/spot/candlesticks', {'currency_pair':symbol,'interval':interval,
                        'from':cursor//1000,'to':(finish-1)//1000})
                found = {}
                try:
                    if not isinstance(rows, list): raise ValueError()
                    for row in rows:
                        stamp = int(row[0]) * (1000 if source == 'gate' else 1)
                        if not cursor <= stamp < finish: continue
                        if source == 'gate':
                            from gate_market import _is_closed_flag, _closed_flag
                            flag = row[7] if len(row) == 8 else row[6] if len(row) == 7 and _is_closed_flag(row[6]) else None
                            if flag is not None and not _closed_flag(flag): continue
                        peak = Decimal(str(row[3] if source == 'gate' else row[2]))
                        if not peak.is_finite() or peak <= 0 or stamp in found: raise ValueError()
                        if source == 'binance' and int(row[6]) != stamp+step-1: raise ValueError()
                        found[stamp] = peak
                    if set(found) != set(range(cursor,finish,step)): raise ValueError()
                except (ValueError,TypeError,IndexError,InvalidOperation):
                    raise MarketError('Incomplete historical high coverage.') from None
                value = str(max(found.values()))
                self.store._set_metadata(segment, value)
            high = max(high, Decimal(value)) if high is not None else Decimal(value)
            cursor = finish
            self.store._set_metadata(key, {'price':str(high),'through':cursor})
        return {'price':str(high),'start':start,'through':end}
