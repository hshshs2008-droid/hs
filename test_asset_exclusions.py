import unittest
from unittest.mock import Mock

from alerts import AlertStore
from gate_market import GateClient
from indicators import Signal
from market import BinanceClient, EXCLUDED_BASE_ASSETS


class AssetExclusionTests(unittest.TestCase):
    def test_binance_excludes_exact_five_without_touching_prior_examples(self):
        allowed = {'BTC', 'EUR', 'CRCLB', 'WENB', 'APP', 'APPB2'}
        client = BinanceClient()
        client._get = Mock(return_value={'symbols': [
            dict(symbol=base+'USDT', status='TRADING', quoteAsset='USDT', isSpotTradingAllowed=True)
            for base in EXCLUDED_BASE_ASSETS | allowed]})
        self.assertEqual(client.spot_symbols(), sorted(base+'USDT' for base in allowed))

    def test_gate_cannot_reintroduce_excluded_binance_assets(self):
        binance = Mock()
        binance.spot_symbols.return_value = ['BTCUSDT']
        client = GateClient(binance)
        client._get = Mock(return_value=[dict(id=base+'_USDT', base=base, quote='USDT', trade_status='tradable')
                                       for base in EXCLUDED_BASE_ASSETS | {'OTHER'}])
        self.assertEqual(client.spot_symbols(), ['OTHER_USDT'])
        for base in EXCLUDED_BASE_ASSETS:
            self.assertFalse(client.is_eligible(base+'_USDT'))

    def test_queued_excluded_alerts_are_retained_but_not_delivered(self):
        store = AlertStore(':memory:')
        self.addCleanup(store.close)
        store.subscribe(1, 0)
        signal = Signal(1000, 29, 31, 10, 18, 14, 16, 1000, 1000)
        for base in EXCLUDED_BASE_ASSETS:
            for suffix in ('USDT', '_USDT'):
                symbol = base+suffix
                store.record(symbol, 1000, signal)
                self.assertFalse(store.is_pending(1, symbol, 1000))
        self.assertEqual(store.pending(), [])
        self.assertEqual(store._db.execute('SELECT count(*) FROM signals').fetchone()[0], 10)
        store.record('EURUSDT', 1000, signal)
        self.assertEqual([row[1] for row in store.pending()], ['EURUSDT'])
