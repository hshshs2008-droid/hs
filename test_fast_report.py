import hashlib
import json
import threading
import unittest
from unittest.mock import Mock
from alerts import AlertStore
from market import MarketError
from peak_prices import PeakPrices, MINUTE, HOUR
from report_quotes import ReportQuotes
from reporting import ReportService
from test_reporting import row, cells, NOW

class FastReportTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.router = Mock()
        self.clock = Mock(return_value=NOW/1000)
        self.quotes = ReportQuotes(self.router,self.stop,clock=self.clock)

    def test_120_assets_two_requests_and_no_unrequested_tickers_stored(self):
        keys={('binance',f'B{i}USDT') for i in range(57)} | {('gate',f'G{i}_USDT') for i in range(63)}
        self.router.binance_client._get.return_value=[{'symbol':f'B{i}USDT','price':'3'} for i in range(57)]
        self.router.gate_client._get.return_value=[{'currency_pair':f'G{i}_USDT','last':'4'} for i in range(3000)]
        result=self.quotes.fetch(keys)
        self.assertEqual(len(result),120)
        self.assertEqual(len(self.quotes.cache),120)
        self.router.binance_client._get.assert_called_once()
        self.router.gate_client._get.assert_called_once()
        self.assertEqual(result,self.quotes.fetch(keys))
        self.assertEqual(self.router.gate_client._get.call_count,1)
        self.clock.return_value=NOW/1000+60
        self.quotes.fetch(keys)
        self.assertEqual(self.router.gate_client._get.call_count,2)

    def test_exchange_identity_duplicates_and_bad_prices(self):
        self.router.binance_client._get.return_value=[{'symbol':'ABCUSDT','price':'10'}]
        self.router.gate_client._get.return_value=[{'currency_pair':'ABC_USDT','last':'2'},
            {'currency_pair':'BAD_USDT','last':'NaN'},
            {'currency_pair':'DUP_USDT','last':'3'},{'currency_pair':'DUP_USDT','last':'4'}]
        result=self.quotes.fetch({('binance','ABCUSDT'),('gate','ABC_USDT'),('gate','BAD_USDT'),('gate','DUP_USDT')})
        self.assertEqual(result[('binance','ABCUSDT')]['price'],'10')
        self.assertEqual(result[('gate','ABC_USDT')]['price'],'2')
        self.assertNotIn(('gate','BAD_USDT'),result)
        self.assertNotIn(('gate','DUP_USDT'),result)

    def test_batch_failure_never_serves_expired_prices_or_individual_retry(self):
        self.router.gate_client._get.return_value=[{'currency_pair':'ABC_USDT','last':'2'}]
        self.quotes.fetch({('gate','ABC_USDT')})
        self.clock.return_value=NOW/1000+61
        self.router.gate_client._get.side_effect=MarketError('cooldown',status=429)
        self.assertEqual(self.quotes.fetch({('gate','ABC_USDT')}),{})
        self.router.gate_client.latest_price.assert_not_called()

    def test_snapshot_no_network_even_while_tracker_lock_is_busy(self):
        db=AlertStore(':memory:');self.addCleanup(db.close)
        provider=PeakPrices(self.router,db,self.stop)
        key='peak_v1:'+hashlib.sha256(json.dumps(['gate','ABC_USDT',MINUTE]).encode()).hexdigest()
        db._set_metadata(key,{'price':'12','through':HOUR})
        locked=threading.Event();release=threading.Event()
        def hold():
            with provider._lock:
                locked.set();release.wait(2)
        thread=threading.Thread(target=hold);thread.start()
        self.assertTrue(locked.wait(1))
        try:
            self.assertEqual(provider.snapshot('gate','ABC_USDT',15000,2*HOUR),{'price':'12','start':MINUTE,'through':HOUR})
        finally:
            release.set();thread.join(2)
        self.router.gate_client._get.assert_not_called()
        db._set_metadata(key,{'price':'12','through':HOUR,'upper':'99'})
        self.assertNotIn('price',provider.snapshot('gate','ABC_USDT',15000,2*HOUR))

    def test_service_sends_cached_peaks_without_historical_scan(self):
        store=Mock();store.can_access.return_value=True
        store.report_rows.return_value=[row()];store.get_ledger_started_at_ms.return_value=NOW-10000
        peak=Mock();peak.snapshot.return_value={'price':'110','start':NOW-90000,'through':NOW-60000}
        self.router.binance_client._get.return_value=[{'symbol':'BTCUSDT','price':'120'}]
        send=Mock()
        service=ReportService(store,self.router,send,Mock(),self.stop,clock=self.clock,
                              peak_provider=peak,quote_provider=self.quotes)
        service._generate(100)
        peak.measure.assert_not_called()
        peak.snapshot.assert_called_once()
        self.router.latest_price.assert_not_called()
        table=cells(send.call_args.args[1])
        self.assertEqual(table[1][7],120)
        self.assertEqual(table[1][9],110)
        self.assertIn('آخر تحديث محفوظ',send.call_args.args[3])

    def test_binance_batches_are_bounded(self):
        self.router.binance_client._get.side_effect=lambda path,p:[{'symbol':s,'price':'1'} for s in json.loads(p['symbols'])]
        result=self.quotes.fetch({('binance',f'B{i}USDT') for i in range(205)})
        self.assertEqual(len(result),205)
        self.assertEqual(self.router.binance_client._get.call_count,3)
        for call in self.router.binance_client._get.call_args_list:
            self.assertLessEqual(len(json.loads(call.args[1]['symbols'])),100)
