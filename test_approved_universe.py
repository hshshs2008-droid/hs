import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from approved_universe import load_universe, configure_store
from market import BinanceClient
from gate_market import GateClient
from alerts import AlertStore

class ApprovedUniverseTests(unittest.TestCase):
    def test_manifest_unique_and_counts(self):
        u, _ = load_universe()
        self.assertEqual((len(u['Binance']),len(u['Gate.io'])), (322,352))

    def test_duplicate_identity_fails_closed(self):
        data=json.loads(Path('approved_universe.json').read_text())
        data['coins'][1]['cmc_id']=data['coins'][0]['cmc_id']
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad.json';p.write_text(json.dumps(data))
            with self.assertRaises(ValueError):load_universe(p)

    def test_binance_intersection(self):
        c=BinanceClient(); c.selected_symbols=frozenset({'BTCUSDT'})
        c._get=Mock(return_value={'symbols':[dict(symbol=s,status='TRADING',isSpotTradingAllowed=True,quoteAsset='USDT') for s in ['BTCUSDT','ETHUSDT']]})
        self.assertEqual(c.spot_symbols(),['BTCUSDT'])

    def test_gate_override_bypasses_binance_and_old_800(self):
        b=Mock(); b.spot_symbols.side_effect=AssertionError('must not query Binance')
        c=GateClient(b);c.selected_symbols=frozenset({'XLM_USDT','BTC_USDT'})
        c._select_fixed=Mock(side_effect=AssertionError('must not select 800'))
        c._get=Mock(return_value=[dict(id=s,base=s[:-5],quote='USDT',trade_status=status,type='normal') for s,status in [('XLM_USDT','tradable'),('BTC_USDT','untradable'),('ETH_USDT','tradable')]])
        self.assertEqual(c.spot_symbols(),['XLM_USDT'])
        self.assertTrue(c.is_eligible('XLM_USDT'))
        self.assertFalse(c.is_eligible('ETH_USDT'))

    def test_transition_persists_floor_and_clears_cache_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'state.db'; s=AlertStore(p)
            s.save_history_cache('complete',{'old':True})
            configure_store(s,{'BTCUSDT'},'v1',now_ms=12345)
            self.assertEqual(s.get_history_cache(),{})
            self.assertFalse(s.is_pending(1,'ETHUSDT',999999))
            s.save_history_cache('complete',{'new':True})
            s._db.close();s=AlertStore(p)
            configure_store(s,{'BTCUSDT'},'v1',now_ms=99999)
            self.assertEqual(s.strategy_activation_ms,12345)
            self.assertEqual(s.get_history_cache(),{'complete':{'new':True}})
            s._db.close()
