import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from alerts import AlertStore
from gate_market import GateClient
from market import BinanceClient, MarketError, HistoricalWindowError
from peak_prices import PeakPrices, PeakTracker, MINUTE, HOUR


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = AlertStore(':memory:')
        self.addCleanup(self.db.close)
        self.stop = Mock()
        self.stop.wait.return_value = False
        self.stop.is_set.return_value = False
        self.client = Mock()
        self.router = Mock(gate_client=self.client)
        self.provider = PeakPrices(self.router,self.db,self.stop)

    def feed(self, boundary=9, complete=12):
        def get(path,p):
            if p['interval']=='1m':
                raise HistoricalWindowError('old',status=400)
            step={'5m':300,'1h':3600}[p['interval']]
            return [[str(t),'0','1',str(boundary if t==0 else complete),'1','1','0','true']
                    for t in range(p['from'],p['to']+1,step)]
        self.client._get.side_effect=get

    def test_old_start_recovered_only_when_full_later_high_dominates(self):
        self.feed()
        result=self.provider.measure('gate','CT_USDT',15000,HOUR)
        self.assertEqual(result,{'price':'12','start':MINUTE,'through':HOUR})
        # The 5m candle at zero includes pre-send prices; never use its high as peak.
        self.assertEqual(self.client._get.call_args_list[1].args[1]['from'],0)
        calls=self.client._get.call_count
        self.provider.measure('gate','CT_USDT',15000,HOUR)
        self.assertEqual(calls,self.client._get.call_count)
        self.assertEqual(self.db._db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'peak_v1:%'").fetchone()[0],1)
        self.assertEqual(self.db._db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'peak_segment_v1:%'").fetchone()[0],0)

    def test_higher_boundary_never_claimed_then_resolves_after_restart(self):
        self.feed(boundary=99,complete=12)
        result=self.provider.measure('gate','CT_USDT',15000,HOUR)
        self.assertNotIn('price',result)
        self.assertIn('error',result)
        provider=PeakPrices(self.router,self.db,self.stop)
        self.feed(boundary=99,complete=100)
        result=provider.measure('gate','CT_USDT',15000,2*HOUR)
        self.assertEqual(result['price'],'100')
        self.assertEqual(self.client._get.call_args.args[1]['from'],3600)

    def test_aligned_coarse_start_needs_no_boundary_exclusion(self):
        self.feed(boundary=99,complete=12)
        result=self.provider.measure('gate','CT_USDT',5*MINUTE,HOUR)
        self.assertEqual(result['price'],'12')

    def test_generic_http400_does_not_enable_coarse_fallback(self):
        self.client._get.side_effect=MarketError('bad pair',status=400)
        with self.assertRaises(MarketError):
            self.provider.measure('gate','CT_USDT',15000,HOUR)
        self.assertEqual(self.client._get.call_count,1)

    def test_missing_coarse_candle_does_not_claim_peak(self):
        def get(path,p):
            if p['interval']=='1m': raise HistoricalWindowError('old',status=400)
            return [[str(t),'0','1','12','1','1'] for t in range(0,3300,300)]
        self.client._get.side_effect=get
        with self.assertRaises(MarketError):
            self.provider.measure('gate','CT_USDT',15000,HOUR)

    def test_legacy_checkpoint_survives_compaction(self):
        import hashlib
        key='peak_v1:'+hashlib.sha256(json.dumps(['gate','CT_USDT',MINUTE]).encode()).hexdigest()
        self.db._set_metadata(key,{'price':'99','through':HOUR})
        self.db._set_metadata('peak_segment_v1:old','123')
        self.db._set_metadata('unrelated','keep')
        self.db.compact_peak_cache()
        self.assertIsNone(self.db._metadata('peak_segment_v1:old',None))
        self.assertEqual(self.db._metadata('unrelated',None),'keep')
        self.feed(complete=12)
        self.assertEqual(self.provider.measure('gate','CT_USDT',15000,2*HOUR)['price'],'99')
        self.assertEqual(self.client._get.call_args.args[1]['interval'],'1h')

    def test_explicit_gate_error_recognition(self):
        client=GateClient(BinanceClient())
        for body,expected in [({'label':'INVALID_PARAM_VALUE','message':'Candlestick too long ago. Maximum 10000 points ago are allowed'},HistoricalWindowError),({'label':'INVALID_PARAM_VALUE','message':'Invalid currency pair'},MarketError)]:
            error=HTTPError('https://api.gateio.ws',400,'Bad Request',{},io.BytesIO(json.dumps(body).encode()))
            with patch('gate_market.urlopen',side_effect=error):
                with self.assertRaises(expected) as result: client._get('/spot/candlesticks')
                self.assertIs(type(result.exception),expected)

    def test_tracker_uses_closed_hours_and_respects_source_cooldown(self):
        rows=[{'symbol':'A_USDT','quote_key':('gate','A_USDT'),'telegram_sent_at_ms':10},
              {'symbol':'B_USDT','quote_key':('gate','B_USDT'),'telegram_sent_at_ms':20},
              {'symbol':'CUSDT','quote_key':('binance','CUSDT'),'telegram_sent_at_ms':30}]
        provider=Mock()
        provider.measure.side_effect=[{'price':'1'},MarketError('limit',status=429)]
        tracker=PeakTracker(provider,lambda:rows,self.stop,clock=lambda:3601)
        tracker.run_once()
        self.assertEqual(provider.measure.call_count,2)
        self.assertEqual(provider.measure.call_args_list[0].args,('binance','CUSDT',30,HOUR))
        self.assertEqual(provider.measure.call_args_list[1].args,('gate','B_USDT',20,HOUR))

    def test_segment_memory_has_fixed_capacity(self):
        self.feed()
        for i in range(520):
            self.provider._segment('gate',str(i),HOUR,2*HOUR,HOUR)
        self.assertEqual(len(self.provider._segments),512)
