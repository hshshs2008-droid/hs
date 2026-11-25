"""Small in-memory quote snapshots; bulk fetch, no persisted ticker history."""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import json
import logging
import time
from market import MarketError

logger = logging.getLogger(__name__)

class ReportQuotes:
    def __init__(self, router, stop, *, clock=time.time):
        self.router, self.stop, self.clock = router, stop, clock
        self.cache = OrderedDict()
        self.errors = {}

    @staticmethod
    def valid(value):
        try:
            price = Decimal(str(value))
            return price.is_finite() and price > 0 and -40 <= price.adjusted() <= 30
        except (InvalidOperation, ValueError, TypeError):
            return False

    def fetch(self, keys, *, not_before=None):
        from reporting import _key_parts
        self.errors = {}
        now = int(self.clock()*1000)
        for key in list(self.cache):
            if not 0 <= now-self.cache[key]['at_ms'] < 60000:
                del self.cache[key]
        not_before = not_before or {}
        result = {key:dict(self.cache[key]) for key in keys if key in self.cache
                  and self.cache[key]['at_ms'] >= not_before.get(key, 0)}
        for source in ('binance','gate'):
            needed = { _key_parts(key)[1]:key for key in keys
                       if key not in result and _key_parts(key)[0] == source }
            if not needed: continue
            client = self.router.binance_client if source=='binance' else self.router.gate_client
            groups = [list(needed)[i:i+100] for i in range(0,len(needed),100)] if source=='binance' else [list(needed)]
            fallback_budget = 8
            for symbols in groups:
                if self.stop.is_set(): return result
                try:
                    if client is None: raise MarketError('Source unavailable')
                    if source=='binance':
                        try:
                            data = self._request(client, '/api/v3/ticker/price', {'symbols':json.dumps(symbols,ensure_ascii=False,separators=(',',':'))})
                        except MarketError as error:
                            if error.status != 400: raise
                            # A delisted/bad symbol must not hide other valid pairs.
                            # One bulk fallback; never retry access/rate failures.
                            data = self._request(client, '/api/v3/ticker/price')
                        name, field = 'symbol','price'
                    else:
                        # Gate supports one pair or all; use one request per report.
                        params = {'currency_pair':symbols[0]} if len(symbols)==1 else None
                        data = self._request(client, '/spot/tickers',params)
                        name, field = 'currency_pair','last'
                    at = int(self.clock()*1000)
                    if not isinstance(data,list): raise MarketError('Invalid quote batch')
                    selected, duplicates = {}, set()
                    for row in data:
                        if not isinstance(row,dict): continue
                        symbol = row.get(name)
                        if not isinstance(symbol,str) or symbol not in needed: continue
                        if symbol in selected: duplicates.add(symbol)
                        selected[symbol] = row.get(field)
                    for symbol in symbols:
                        value = selected.get(symbol)
                        if symbol in duplicates or not self.valid(value):
                            self.errors[needed[symbol]] = 'لم تُرجع المنصة سعرًا صالحًا لهذا الزوج'
                            continue
                        key = needed[symbol]
                        result[key] = {'price':str(value),'at_ms':at}
                        self.cache[key] = dict(result[key])
                        self.cache.move_to_end(key)
                        if len(self.cache)>2048: self.cache.popitem(last=False)
                    # Recover omissions from Gate's bulk list with exact-pair requests.
                    if source == 'gate':
                        for symbol in sorted(symbols):
                            key = needed[symbol]
                            if key in result or fallback_budget <= 0: continue
                            if self.stop.wait(.15): return result
                            fallback_budget -= 1
                            try:
                                items = client._get('/spot/tickers', {'currency_pair':symbol})
                                matches = [item for item in items if isinstance(item,dict)
                                           and item.get('currency_pair') == symbol] if isinstance(items,list) else []
                                if len(matches)!=1 or not self.valid(matches[0].get('last')):
                                    logger.warning('Report quote missing; source=gate symbol=%s reason=missing_or_invalid_ticker',symbol)
                                    continue
                                quote = {'price':str(matches[0]['last']), 'at_ms':int(self.clock()*1000)}
                                result[key] = quote
                                self.cache[key] = dict(quote)
                                self.cache.move_to_end(key)
                                if len(self.cache)>2048: self.cache.popitem(last=False)
                                self.errors.pop(key,None)
                            except MarketError as error:
                                self.errors[key] = 'فشل طلب سعر الزوج'+(f' (HTTP {error.status})' if error.status else '')
                                logger.warning('Report quote recovery failed; source=gate symbol=%s status=%s',symbol,error.status)
                                if error.status in (403,418,429,451): break
                except MarketError as error:
                    for symbol in symbols:
                        self.errors[needed[symbol]] = 'فشل طلب الأسعار'+(f' (HTTP {error.status})' if error.status else '')
                    logger.warning('Report batch unavailable; source=%s status=%s',source,error.status)
                    # Do not turn one failed batch into hundreds of retries.
                    break
        return result

    def _request(self, client, path, params=None):
        """One retry for transport/server failures; never bypass exchange cooldowns."""
        try:
            return client._get(path, params) if params is not None else client._get(path)
        except MarketError as error:
            if (error.status is not None and error.status != 408 and not 500<=error.status<600):
                raise
            if self.stop.wait(1): raise
            return client._get(path, params) if params is not None else client._get(path)
