import unittest
from unittest.mock import Mock
from alerts import AlertStore
from market import MarketError
from peak_prices import PeakPrices, MINUTE, HOUR
from reporting import build_report
from test_reporting import cells, row, NOW
from decimal import Decimal

class PeakTests(unittest.TestCase):
    def test_complete_minutes_exclude_prior_high_and_cache_incrementally(self):
        for source in ('binance','gate'):
            with self.subTest(source=source):
                db=AlertStore(':memory:'); self.addCleanup(db.close)
                client=Mock(); router=Mock(binance_client=client,gate_client=client)
                stop=Mock();stop.wait.return_value=False
                def get(path,p):
                    a=p['startTime'] if source=='binance' else p['from']*1000
                    b=p['endTime']+1 if source=='binance' else (p['to']+1)*1000
                    step=HOUR if p['interval']=='1h' else MINUTE
                    self.assertGreaterEqual(a,MINUTE)
                    return [[t,'1','12','1','1','0',t+step-1] if source=='binance'
                            else [str(t//1000),'0','1','12','1','1'] for t in range(a,b,step)]
                client._get.side_effect=get
                provider=PeakPrices(router,db,stop)
                result=provider.measure(source,'BTCUSDT',15000,3*HOUR+45000)
                self.assertEqual(result,{'price':'12','start':MINUTE,'through':3*HOUR})
                calls=client._get.call_count
                provider.measure(source,'BTCUSDT',15000,3*HOUR+45000)
                self.assertEqual(client._get.call_count,calls)
                provider.measure(source,'BTCUSDT',15000,3*HOUR+MINUTE)
                self.assertEqual(client._get.call_count,calls+1)
    def test_missing_coverage_does_not_claim_a_peak(self):
        db=AlertStore(':memory:');self.addCleanup(db.close)
        router=Mock();router.binance_client._get.return_value=[]
        stop=Mock();stop.wait.return_value=False
        with self.assertRaises(MarketError):
            PeakPrices(router,db,stop).measure('binance','BTCUSDT',15000,2*MINUTE)
    def test_peak_return_and_current_loss_both_visible(self):
        r=row(entry='100',peak={'price':'110','start':NOW-60000,'through':NOW})
        table=cells(build_report([r],{'BTCUSDT':{'price':'95','at_ms':NOW}},NOW))
        self.assertEqual(table[1][8],Decimal('-.05'))
        self.assertEqual(table[1][9:], [Decimal('110'),Decimal('.1')])
