import io
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from gate_market import GateClient
from market import BinanceClient, DelistedMarketError, MarketError
from report_quotes import ReportQuotes
from peak_prices import PeakPrices
from reporting import build_report, ReportService
from test_reporting import row, cells, NOW

class DelistingTests(unittest.TestCase):
    def test_explicit_delisting_only_matches_requested_currency(self):
        client=GateClient(BinanceClient())
        for label,message,expected in [
            ('INVALID_CURRENCY','currency LION is delisted',DelistedMarketError),
            ('INVALID_CURRENCY','currency OTHER is delisted',MarketError),
            ('INVALID_CURRENCY','Invalid currency',MarketError),
            ('OTHER','currency LION is delisted',MarketError)]:
            error=HTTPError('https://api.gateio.ws',400,'bad',{},io.BytesIO(json.dumps({'label':label,'message':message}).encode()))
            with patch('gate_market.urlopen',side_effect=error):
                with self.assertRaises(expected) as caught:
                    client._get('/spot/tickers',{'currency_pair':'LION_USDT'})
            self.assertIs(type(caught.exception),expected)

    def test_bulk_omission_and_individual_delisting_are_classified(self):
        client=Mock();client._get.side_effect=[[],DelistedMarketError('delisted',status=400)]
        provider=ReportQuotes(Mock(gate_client=client),threading.Event())
        keys={('gate','LION_USDT'),('gate','VVS_USDT')}
        # Both explicit pair rejections, with no retries of permanent errors.
        client._get.side_effect=[[],DelistedMarketError('delisted',status=400),DelistedMarketError('delisted',status=400)]
        self.assertEqual(provider.fetch(keys),{})
        self.assertEqual(provider.delisted,keys)
        self.assertEqual(client._get.call_count,3)

    def test_archival_peak_is_not_a_current_complete_price(self):
        archived=PeakPrices._archive(NOW-60000,{'price':'125','through':NOW-1000})
        self.assertEqual(archived['archived_price'],'125')
        self.assertNotIn('price',archived)
        record=row(peak=archived)
        document=build_report([record],{'BTCUSDT':{'delisted':True}},NOW,None)
        table=cells(document)
        self.assertEqual(table[1][7],'مشطوبة')
        self.assertEqual(table[1][9],'125 (تاريخي)')
        self.assertEqual(table[1][10],'تاريخي فقط')
        self.assertIn('آخر قمة تاريخية محفوظة',str(cells(document,'sheet2')))
        self.assertNotIn('archived_price',PeakPrices._archive(NOW,{}))

    def test_service_propagates_delisting_to_peak_provider(self):
        store=Mock();store.can_access.return_value=True;store.report_rows.return_value=[row()]
        store.get_ledger_started_at_ms.return_value=NOW
        quotes=Mock();quotes.fetch.return_value={};quotes.errors={'BTCUSDT':'مشطوبة'};quotes.delisted={'BTCUSDT'}
        peak=Mock();send=Mock()
        service=ReportService(store,Mock(),send,Mock(),threading.Event(),clock=lambda:NOW/1000,peak_provider=peak,quote_provider=quotes)
        service._generate(100)
        self.assertTrue(peak.refresh_report.call_args.args[1]['BTCUSDT']['delisted'])
        self.assertEqual(cells(send.call_args.args[1])[1][7],'مشطوبة')
