import io
import json
import math
import threading
import unittest
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import gate_market
from gate_market import GateClient
from market import Candle, FOUR_HOURS_MS, MarketError


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def pair(base='GATEONLY', **extra):
    return dict(id=base + '_USDT', base=base, quote='USDT', trade_status='tradable', type='normal', **extra)


def candle(index, closed='true'):
    return [str(index * FOUR_HOURS_MS // 1000), '100', '11', '12', '8', '10', '9', closed]


class GateMarketTests(unittest.TestCase):
    def setUp(self):
        self.binance = Mock()
        self.binance.spot_symbols.return_value = ['BTCUSDT', 'ETHUSDT']
        self.client = GateClient(self.binance)

    @patch('gate_market.urlopen')
    def test_public_only_endpoints_and_documented_limit_without_from_or_to(self, urlopen):
        urlopen.side_effect = [response({'server_time': 1792915200123}), response([pair()]),
                               response([candle(0)]), response([{'currency_pair':'GATEONLY_USDT','last':'0.0100'}])]
        self.assertEqual(self.client.server_time(), 1792915200123)
        self.assertEqual(self.client.spot_symbols(), ['GATEONLY_USDT'])
        self.assertEqual(len(self.client.candles('GATEONLY_USDT', FOUR_HOURS_MS)), 1)
        self.assertEqual(self.client.latest_price('GATEONLY_USDT'), '0.01')
        paths=[call.args[0].full_url for call in urlopen.call_args_list]
        self.assertEqual(paths, ['https://api.gateio.ws/api/v4/spot/time',
                                 'https://api.gateio.ws/api/v4/spot/currency_pairs',
                                 'https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=GATEONLY_USDT&interval=4h&limit=500',
                                 'https://api.gateio.ws/api/v4/spot/tickers?currency_pair=GATEONLY_USDT'])
        for call in urlopen.call_args_list:
            self.assertIsNone(call.args[0].data)
            self.assertIsNone(call.args[0].get_header('Authorization'))
            self.assertLessEqual(call.kwargs['timeout'],20)

    @patch.object(GateClient, '_get')
    def test_spot_universe_excludes_binance_nontradable_and_premarket(self, get):
        get.return_value = [pair(), pair(), pair('BTC'), pair('ETH'), pair('币安人生'), pair('OLD_V2'), pair('OLD-V3'), pair('TOKEN.V4'),
                            dict(pair('OTHER'),quote='BTC',id='OTHER_BTC'),
                            dict(pair('WAIT'),trade_status='buyable'),
                            dict(pair('OLD'),trade_status='sellable'),
                            dict(pair('NO'),trade_status='untradable'),
                            dict(pair('PRE'),type='premarket')]
        self.assertEqual(self.client.spot_symbols(),['GATEONLY_USDT','OLD-V3_USDT','OLD_V2_USDT','TOKEN.V4_USDT','币安人生_USDT'])
        self.binance.spot_symbols.return_value += ['GATEONLYUSDT']
        self.assertEqual(self.client.spot_symbols(),['OLD-V3_USDT','OLD_V2_USDT','TOKEN.V4_USDT','币安人生_USDT'])
        self.assertEqual(self.binance.spot_symbols.call_count, 2)

    @patch.object(GateClient, '_get')
    def test_binance_lookup_failure_is_fail_closed_before_gate_discovery(self, get):
        for universe in ([], None, 'BTCUSDT', ['BROKEN'], [True]):
            with self.subTest(universe=universe), self.assertRaises(MarketError):
                self.binance.spot_symbols.return_value = universe
                self.client.spot_symbols()
        self.binance.spot_symbols.side_effect = MarketError('Binance unavailable',status=429)
        with self.assertRaises(MarketError): self.client.spot_symbols()
        get.assert_not_called()

    @patch.object(GateClient, '_get')
    def test_invalid_exchange_information_and_pair_identity_are_rejected(self, get):
        for value in ({}, [None], [dict(pair(),id='OTHER_USDT')], [dict(pair(),base=None)], [dict(pair(),id='GATEONLY/USDT')]):
            with self.subTest(value=value), self.assertRaises(MarketError):
                get.return_value=value
                self.client.spot_symbols()
        get.return_value=[pair('BTC'),pair('ETH')]
        self.assertEqual(self.client.spot_symbols(),[])
        self.assertIs(self.client.allow_empty_universe,True)
        get.return_value=[]
        with self.assertRaises(MarketError):self.client.spot_symbols()

    @patch.object(GateClient, '_get')
    def test_eligibility_snapshot_starts_closed_and_refreshes_atomically(self, get):
        self.assertFalse(self.client.is_eligible('OLD-V2_USDT'))
        self.assertFalse(self.client.is_eligible('GATEONLY_USDT'))
        self.assertFalse(self.client.is_eligible({'native':'OLD-V2_USDT'}))
        get.assert_not_called()
        self.binance.spot_symbols.assert_not_called()
        get.return_value = [pair('OLD-V2'), pair('GATEONLY'), pair('BTC')]
        self.assertEqual(self.client.spot_symbols(), ['GATEONLY_USDT','OLD-V2_USDT'])
        self.assertTrue(self.client.is_eligible('OLD-V2_USDT'))
        self.assertTrue(self.client.is_eligible('GATEONLY_USDT'))
        self.assertFalse(self.client.is_eligible('BTC_USDT'))
        calls = get.call_count
        self.assertTrue(self.client.is_eligible('OLD-V2_USDT'))
        self.assertEqual(get.call_count, calls)
        self.binance.spot_symbols.return_value = ['BTCUSDT','ETHUSDT','GATEONLYUSDT']
        self.client.spot_symbols()
        self.assertFalse(self.client.is_eligible('GATEONLY_USDT'))
        self.assertTrue(self.client.is_eligible('OLD-V2_USDT'))
        self.assertFalse(self.client.is_eligible('OLDV2_USDT'))

    @patch.object(GateClient, '_get')
    def test_failed_discovery_does_not_replace_the_last_successful_snapshot(self, get):
        get.return_value = [pair('GATEONLY')]
        self.client.spot_symbols()
        self.assertTrue(self.client.is_eligible('GATEONLY_USDT'))
        get.return_value = [pair('NEW'), dict(pair('BROKEN'),id='WRONG_USDT')]
        with self.assertRaises(MarketError): self.client.spot_symbols()
        self.assertTrue(self.client.is_eligible('GATEONLY_USDT'))
        self.assertFalse(self.client.is_eligible('NEW_USDT'))
        self.binance.spot_symbols.side_effect = MarketError('Exclusion lookup unavailable')
        with self.assertRaises(MarketError): self.client.spot_symbols()
        self.assertTrue(self.client.is_eligible('GATEONLY_USDT'))

    @patch.object(GateClient, '_get')
    def test_latest_quote_keeps_precision_and_does_not_require_current_universe(self,get):
        for raw,expected in (('0.000000010000','0.00000001'),('25000.000','25000'),('1.123456789123456789','1.123456789123456789')):
            get.return_value=[{'currency_pair':'OLD_V2_USDT','last':raw}]
            self.assertEqual(self.client.latest_price('OLD_V2_USDT'),expected)
        self.binance.spot_symbols.assert_not_called()

    @patch.object(GateClient, '_get')
    def test_latest_quote_requires_exact_pair_and_positive_finite_decimal(self,get):
        for raw in (None, True, 1, '0', '-1', 'NaN', 'sNaN', 'Infinity', '1e99999', '1e-9999'):
            with self.subTest(raw=raw), self.assertRaises(MarketError):
                get.return_value=[{'currency_pair':'GATEONLY_USDT','last':raw}]
                self.client.latest_price('GATEONLY_USDT')
        for value in ([], {}, [{'currency_pair':'OTHER_USDT','last':'1'}], [{'currency_pair':'GATEONLY_USDT','last':'1'}]*2):
            with self.subTest(value=value), self.assertRaises(MarketError):
                get.return_value=value
                self.client.latest_price('GATEONLY_USDT')

    @patch.object(GateClient, '_get')
    def test_server_time_modern_ms_and_legacy_explicit_seconds(self,get):
        for value,expected in (({'server_time':1792915200123},1792915200123),
                               ({'server_time':1792915200},1792915200000),
                               ({'server_time':1792915200,'server_time_ms':1792915200123},1792915200123)):
            get.return_value=value
            self.assertEqual(self.client.server_time(),expected)
        for value in ({}, [], {'server_time':True}, {'server_time':'1792915200'}, {'server_time':42},
                      {'server_time_ms':1792915200}, {'server_time':-1}, {'server_time':10**18}):
            with self.subTest(value=value), self.assertRaises(MarketError):
                get.return_value=value
                self.client.server_time()

    @patch.object(GateClient,'_get')
    def test_closed_ohlc_fields_and_arbitrary_api_order_are_canonicalized(self,get):
        get.return_value=[candle(2),candle(0),candle(1)]
        data=self.client.candles('GATEONLY_USDT',3*FOUR_HOURS_MS)
        self.assertEqual([item.open_time for item in data],[0,FOUR_HOURS_MS,2*FOUR_HOURS_MS])
        self.assertEqual((data[0].open,data[0].high,data[0].low,data[0].close),(10,12,8,11))
        self.assertEqual(data[-1].close_time,3*FOUR_HOURS_MS-1)

    @patch.object(GateClient,'_get')
    def test_open_candles_false_flag_and_exact_boundary_are_excluded(self,get):
        get.return_value=[candle(0),candle(1),candle(2)]
        self.assertEqual(len(self.client.candles('GATEONLY_USDT',2*FOUR_HOURS_MS)),2)
        self.assertEqual(len(self.client.candles('GATEONLY_USDT',2*FOUR_HOURS_MS-1)),1)
        get.return_value=[candle(0),candle(1,'false')]
        self.assertEqual(len(self.client.candles('GATEONLY_USDT',3*FOUR_HOURS_MS)),1)
        get.return_value=[candle(0,False)]
        self.assertEqual(self.client.candles('GATEONLY_USDT',2*FOUR_HOURS_MS),[])

    @patch.object(GateClient,'_get')
    def test_optional_older_row_shapes_and_short_histories(self,get):
        for value in ([], [candle(0)[:6]], [candle(0)[:7]], [candle(0)[:6]+['true']]):
            get.return_value=value
            self.assertEqual(len(self.client.candles('GATEONLY_USDT',2*FOUR_HOURS_MS)),len(value))

    @patch.object(GateClient,'_get')
    def test_gaps_duplicate_time_wrong_units_and_wrong_intervals_are_rejected(self,get):
        invalid=candle(0); invalid[0]='1'
        millisecond=candle(0); millisecond[0]='1792915200000'
        for value in ([candle(0),candle(2)], [candle(0),candle(0)], [invalid], [millisecond],
                      [candle(0),candle(1,'false'),candle(2)]):
            with self.subTest(value=value), self.assertRaises(MarketError):
                get.return_value=value
                self.client.candles('GATEONLY_USDT',5*FOUR_HOURS_MS)

    @patch.object(GateClient,'_get')
    def test_bad_prices_volumes_flag_and_rows_are_rejected(self,get):
        for index, value in ((0,True),(1,'-1'),(1,'NaN'),(2,'NaN'),(2,'13'),(3,'7'),(4,'0'),(5,None),(6,'Infinity'),(7,'maybe')):
            bad=candle(0); bad[index]=value
            with self.subTest(index=index,value=value),self.assertRaises(MarketError):
                get.return_value=[bad]
                self.client.candles('GATEONLY_USDT',FOUR_HOURS_MS)
        for value in ({},[None],[[0,'1']],[candle(0)+['extra']]):
            with self.subTest(value=value),self.assertRaises(MarketError):
                get.return_value=value
                self.client.candles('GATEONLY_USDT',FOUR_HOURS_MS)

    @patch.object(GateClient,'_get')
    def test_invalid_arguments_never_make_requests(self,get):
        for symbol,when,limit in (('BAD/USDT',1,500),('BTCUSDT',1,500),('ABC_USDT',0,500),('ABC_USDT',True,500),('ABC_USDT',1,True),('ABC_USDT',1,1001)):
            with self.subTest(symbol=symbol,when=when,limit=limit),self.assertRaises(ValueError):
                self.client.candles(symbol,when,limit)
        get.assert_not_called()

    def test_rejects_credentials_nonhttps_paths_and_invalid_config(self):
        for base in ('http://api.gateio.ws','https://key@api.gateio.ws','https://api.gateio.ws/api/v4','https://api.gateio.ws?token=secret'):
            with self.assertRaises(MarketError):GateClient(self.binance,base)
        for timeout in (0,21,True,math.nan):
            with self.assertRaises(ValueError):GateClient(self.binance,timeout=timeout)
        with self.assertRaises(ValueError):GateClient(self.binance,clock=None)

    @patch('gate_market.urlopen')
    def test_http_cooldown_is_shared_across_gate_endpoints_and_not_binance(self,urlopen):
        now=[1000.0]
        client=GateClient(self.binance,clock=lambda:now[0])
        headers=Message(); headers['Retry-After']='10000'
        urlopen.side_effect=[HTTPError('https://api.gateio.ws/private',429,'private',headers,io.BytesIO(b'private')),
                             response({'server_time':1792915200000})]
        with self.assertRaises(MarketError) as caught:client.server_time()
        self.assertEqual(caught.exception.cooldown_until,11000)
        self.assertNotIn('private',str(caught.exception))
        for method in (client.server_time,lambda:client.latest_price('GATEONLY_USDT'),lambda:client.candles('GATEONLY_USDT',FOUR_HOURS_MS)):
            with self.assertRaises(MarketError) as blocked:method()
            self.assertEqual(blocked.exception.status,429)
        self.assertEqual(urlopen.call_count,1)
        self.assertEqual(self.binance.spot_symbols(),['BTCUSDT','ETHUSDT'])
        now[0]=11000
        self.assertEqual(client.server_time(),1792915200000)
        self.assertEqual(urlopen.call_count,2)

    @patch('gate_market.urlopen')
    def test_network_invalid_json_truncated_and_oversized_responses_are_sanitized(self,urlopen):
        for error in (URLError('private connection details'),IncompleteRead(b'private',10)):
            urlopen.side_effect=error
            with self.assertRaises(MarketError) as caught:self.client.server_time()
            self.assertNotIn('private',str(caught.exception))
        urlopen.side_effect=None
        urlopen.return_value=io.BytesIO(b'private invalid json')
        with self.assertRaises(MarketError) as caught:self.client.server_time()
        self.assertNotIn('private',str(caught.exception))
        urlopen.return_value=response({'server_time':1792915200000})
        with patch('gate_market.MAX_RESPONSE_BYTES',16),self.assertRaisesRegex(MarketError,'oversized'):
            self.client.server_time()


class GateCheckTests(unittest.TestCase):
    @patch('gate_market.GateClient')
    @patch('gate_market.BinanceClient')
    def test_check_reads_only_one_sample_and_prints_safe_success(self, binance, factory):
        now = 1792915200000 + 15_000
        client = factory.return_value
        client.server_time.return_value = now
        client.spot_symbols.return_value = ['GATEONLY_USDT']
        client.latest_price.return_value = '1.01'
        client.candles.return_value = [
            Candle(opened, opened + FOUR_HOURS_MS - 1, 10, 12, 8, 11)
            for opened in range(now - 15_000 - 30 * FOUR_HOURS_MS, now - 15_000, FOUR_HOURS_MS)
        ]
        with patch.dict('os.environ', {'API_GATE_BASE':'https://api.gateio.ws'}), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(gate_market.main(['--check']), 0)
        self.assertIn('GATE_OK eligible_spot_usdt_pairs=1', output.getvalue())
        factory.assert_called_once_with(binance.return_value, base_url='https://api.gateio.ws')
        client.candles.assert_called_once_with('BTC_USDT', now, 30)
        client.latest_price.assert_called_once_with('BTC_USDT')

    @patch('gate_market.GateClient')
    @patch('gate_market.BinanceClient')
    def test_check_is_explicit_and_default_does_not_connect(self, binance, factory):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(gate_market.main([]), 0)
        self.assertIn('--check', output.getvalue())
        factory.assert_not_called()
        binance.assert_not_called()

    @patch('gate_market.GateClient')
    @patch('gate_market.BinanceClient')
    def test_failed_probe_does_not_print_raw_exception_or_response(self, binance, factory):
        factory.return_value.server_time.side_effect = MarketError('private details', status=403)
        with patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(gate_market.main(['--check']), 1)
        self.assertIn('GATE_CHECK_FAILED error_type=MarketError http_status=403', error.getvalue())
        self.assertNotIn('private details', error.getvalue())
        factory.return_value.spot_symbols.assert_not_called()

    @patch('gate_market.GateClient')
    @patch('gate_market.BinanceClient')
    def test_check_rejects_short_or_stale_sample(self, binance, factory):
        client = factory.return_value
        client.server_time.return_value = 1792915200000 + 15_000
        client.spot_symbols.return_value = ['GATEONLY_USDT']
        client.latest_price.return_value = '1.0'
        for candles in ([], [Candle(i * FOUR_HOURS_MS, (i+1) * FOUR_HOURS_MS-1, 10, 12, 8, 11) for i in range(30)]):
            client.candles.return_value = candles
            with patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(gate_market.main(['--check']), 1)


if __name__=='__main__':
    unittest.main()
