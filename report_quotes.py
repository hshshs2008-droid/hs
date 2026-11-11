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

    @staticmethod
    def valid(value):
        try:
            price = Decimal(str(value))
            return price.is_finite() and price > 0 and -40 <= price.adjusted() <= 30
        except (InvalidOperation, ValueError, TypeError):
            return False

    def fetch(self, keys):
        from reporting import _key_parts
        now = int(self.clock()*1000)
        for key in list(self.cache):
            if not 0 <= now-self.cache[key]['at_ms'] < 60000:
                del self.cache[key]
        result = {key:dict(self.cache[key]) for key in keys if key in self.cache}
        for source in ('binance','gate'):
            needed = { _key_parts(key)[1]:key for key in keys
                       if key not in result and _key_parts(key)[0] == source }
            if not needed: continue
            client = self.router.binance_client if source=='binance' else self.router.gate_client
            groups = [list(needed)[i:i+100] for i in range(0,len(needed),100)] if source=='binance' else [list(needed)]
            for symbols in groups:
                if self.stop.is_set(): return result
                try:
                    if client is None: raise MarketError('Source unavailable')
                    if source=='binance':
                        data = client._get('/api/v3/ticker/price', {'symbols':json.dumps(symbols,separators=(',',':'))})
                        name, field = 'symbol','price'
                    else:
                        # Gate supports one pair or all; use one request per report.
                        params = {'currency_pair':symbols[0]} if len(symbols)==1 else None
                        data = client._get('/spot/tickers',params)
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
                        if symbol in duplicates or not self.valid(value): continue
                        key = needed[symbol]
                        result[key] = {'price':str(value),'at_ms':at}
                        self.cache[key] = dict(result[key])
                        self.cache.move_to_end(key)
                        if len(self.cache)>2048: self.cache.popitem(last=False)
                except MarketError as error:
                    logger.warning('Report batch unavailable; source=%s status=%s',source,error.status)
                    # Do not turn one failed batch into hundreds of retries.
                    break
        return result
