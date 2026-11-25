import hashlib
import json
import threading
import unittest
from unittest.mock import Mock
from alerts import AlertStore
from market import MarketError
from peak_prices import PeakPrices, MINUTE, HOUR

class PeakTailTests(unittest.TestCase):
    def setUp(self):
        self.db=AlertStore(':memory:');self.addCleanup(self.db.close)
        self.stop=Mock();self.stop.wait.return_value=False;self.stop.is_set.return_value=False
        self.client=Mock();self.provider=PeakPrices(Mock(gate_client=self.client,binance_client=self.client),self.db,self.stop)
        self.now=2*HOUR+15000
        self.clock=lambda:self.now/1000

    def row(self,sent=15000,source='gate'):
        return {'symbol':'ABC_USDT','quote_key':(source,'ABC_USDT'),'telegram_sent_at_ms':sent}

    def key(self,row):
        start=(row['telegram_sent_at_ms']+MINUTE-1)//MINUTE*MINUTE
        return 'peak_v1:'+hashlib.sha256(json.dumps([row['quote_key'][0],row['symbol'],start]).encode()).hexdigest()

    def save(self,row,price='10',through=HOUR,upper=None):
        self.db._set_metadata(self.key(row),{'price':price,'through':through,'upper':upper})

    def feed(self,live='15',history='12',omit=None,source='gate'):
        def get(path,p):
            start=p['from']*1000 if source=='gate' else p['startTime']
            end=self.now//MINUTE*MINUTE
            records=[]
            for t in range(start,end+MINUTE,MINUTE):
                if t==omit:continue
                high=live if t==end else history
                records.append([str(t//1000),'0','1',high,'1','1','0',t<end] if source=='gate' else [t,'1',high,'1','1','0',t+MINUTE-1])
            return records
        self.client._get.side_effect=get

    def test_fetch_only_missing_tail_and_use_real_high_above_current(self):
        row=self.row();self.save(row,price='10');self.feed(live='18')
        self.provider.refresh_report([row],{row['quote_key']:{'price':'16','at_ms':self.now}},clock=self.clock)
        self.assertEqual(row['peak']['price'],'18')
        self.assertEqual(self.client._get.call_args.args[1]['from'],3600)
        saved=self.db._metadata(self.key(row),None)
        self.assertEqual(saved['through'],2*HOUR)
        self.assertEqual(saved['price'],'12') # Never mark the live candle completed.

    def test_current_price_is_an_observation_not_a_substitute_for_missing_history(self):
        row=self.row();self.save(row);self.feed(live='13')
        self.provider.refresh_report([row],{row['quote_key']:{'price':'16','at_ms':self.now}},clock=self.clock)
        self.assertEqual(row['peak']['price'],'16')
        self.feed(omit=HOUR+MINUTE)
        self.save(row)
        self.provider.refresh_report([row],{row['quote_key']:{'price':'99','at_ms':self.now}},clock=self.clock)
        self.assertNotIn('price',row['peak'])

    def test_repeated_signals_share_one_request_but_keep_distinct_baselines(self):
        rows=[self.row(),self.row(30*MINUTE)]
        self.save(rows[0],price='20');self.save(rows[1],price='10',through=HOUR+MINUTE)
        self.feed()
        self.provider.refresh_report(rows,{},clock=self.clock)
        self.client._get.assert_called_once()
        self.assertEqual(rows[0]['peak']['price'],'20')
        self.assertEqual(rows[1]['peak']['price'],'15')

    def test_new_signal_excludes_pre_send_minute(self):
        row=self.row(HOUR+15000);self.feed()
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertEqual(self.client._get.call_args.args[1]['from'],3660)
        self.assertEqual(row['peak']['start'],HOUR+MINUTE)

    def test_old_uncached_history_stays_in_background(self):
        self.now=24*HOUR
        row=self.row();self.feed()
        self.provider.refresh_report([row],{},clock=self.clock)
        self.client._get.assert_not_called()
        self.assertNotIn('price',row['peak'])

    def test_unknown_old_upper_bound_cannot_be_discarded(self):
        row=self.row();self.save(row,upper='99');self.feed()
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertNotIn('price',row['peak'])
        self.assertEqual(self.db._metadata(self.key(row),None)['upper'],'99')

    def test_live_snapshot_can_prove_bound_without_marking_it_completed(self):
        row=self.row();self.save(row,upper='99');self.feed(live='100')
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertEqual(row['peak']['price'],'100')
        self.assertEqual(self.db._metadata(self.key(row),None)['upper'],'99')

    def test_older_tracker_cannot_replay_or_rewind_report_checkpoint(self):
        row=self.row();self.save(row,through=HOUR+MINUTE)
        self.provider.measure('gate','ABC_USDT',15000,HOUR)
        self.client._get.assert_not_called()
        self.provider._commit(self.key(row),{'price':'8','through':HOUR})
        self.assertEqual(self.db._metadata(self.key(row),None)['through'],HOUR+MINUTE)

    def test_binance_live_tail_and_api_failure(self):
        row=self.row(source='binance');self.save(row);self.feed(source='binance')
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertEqual(row['peak']['price'],'15')
        self.client._get.side_effect=MarketError('timeout')
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertNotIn('price',row['peak'])

    def test_retains_observed_current_price_across_reports(self):
        row=self.row();self.save(row);self.feed(live='13')
        self.provider.refresh_report([row],{row['quote_key']:{'price':'16','at_ms':self.now}},clock=self.clock)
        self.now+=MINUTE
        self.provider.refresh_report([row],{row['quote_key']:{'price':'12','at_ms':self.now}},clock=self.clock)
        self.assertEqual(row['peak']['price'],'16')
        self.assertEqual(self.db._metadata(self.key(row),None)['observed_high'],'16')

    def test_delayed_gate_finalization_is_observed_then_rechecked(self):
        row=self.row();self.save(row);self.feed()
        original=self.client._get.side_effect
        pending=2*HOUR-MINUTE
        def delayed(path,params):
            data=original(path,params)
            for candle in data:
                if int(candle[0])*1000==pending:
                    candle[7]='false';candle[3]='21'
            return data
        self.client._get.side_effect=delayed
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertEqual(row['peak']['price'],'21')
        self.assertIn('note',row['peak'])
        saved=self.db._metadata(self.key(row),None)
        self.assertEqual(saved['through'],pending)
        self.assertEqual(saved['price'],'12')
        self.assertEqual(saved['observed_high'],'21')
        self.feed(history='21')
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertEqual(self.client._get.call_args.args[1]['from'],pending//1000)
        self.assertEqual(self.db._metadata(self.key(row),None)['through'],2*HOUR)
        self.assertNotIn('note',row['peak'])

    def test_old_unclosed_candle_is_still_rejected(self):
        row=self.row();self.save(row);self.feed()
        original=self.client._get.side_effect
        def stale(path,params):
            data=original(path,params);data[0][7]='false';return data
        self.client._get.side_effect=stale
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertNotIn('price',row['peak'])
        self.assertEqual(self.db._metadata(self.key(row),None)['through'],HOUR)

    def test_platforms_fetch_concurrently(self):
        barrier=threading.Barrier(2)
        def client_for(source):
            client=Mock()
            def get(path,params):
                barrier.wait(timeout=3)
                t=2*HOUR
                return [[str(t//1000),'0','1','15','1','1','0',False]] if source=='gate' else [[t,'1','15','1','1','0',t+MINUTE-1]]
            client._get.side_effect=get
            return client
        self.provider.router.gate_client=client_for('gate')
        self.provider.router.binance_client=client_for('binance')
        rows=[self.row(source='gate'),self.row(source='binance')]
        for row in rows:self.save(row,through=2*HOUR)
        self.provider.refresh_report(rows,{},clock=self.clock)
        self.assertEqual([row['peak']['price'] for row in rows],['15','15'])

    def test_transient_missing_live_minute_recovers_without_losing_saved_peak(self):
        row=self.row(source='binance');self.save(row,price='23');self.feed(source='binance')
        normal=self.client._get.side_effect
        calls=[]
        def fetch(path,params):
            data=normal(path,params);calls.append(params)
            return data[:-1] if len(calls)==1 else data
        self.client._get.side_effect=fetch
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertEqual(row['peak']['price'],'23')
        self.assertEqual(len(calls),2)
        self.assertEqual(self.db._metadata(self.key(row),None)['through'],2*HOUR)

    def test_persistent_gap_reports_reason_and_never_advances_coverage(self):
        row=self.row();self.save(row);self.feed(omit=HOUR+MINUTE)
        self.provider.refresh_report([row],{},clock=self.clock)
        self.assertNotIn('price',row['peak'])
        self.assertIn('شموع دقائق ناقصة',row['peak']['error'])
        self.assertEqual(self.client._get.call_count,2)
        self.assertEqual(self.db._metadata(self.key(row),None)['through'],HOUR)

    def test_rate_limit_blocks_tail_retries(self):
        row=self.row();self.save(row)
        self.client._get.side_effect=MarketError('limited',status=429)
        self.provider.refresh_report([row],{},clock=self.clock)
        self.client._get.assert_called_once()
        self.assertIn('HTTP 429',row['peak']['error'])
