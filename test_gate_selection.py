import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gate_market import GateClient
from market import MarketError


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'selection.sqlite3')
        self.day = 1793059200 // 86400 * 86400

    def client(self):
        client = GateClient(Mock())
        client.selection_path = self.path
        client.server_time = Mock(return_value=self.day * 1000)
        return client

    def rows(self, volumes):
        return [[str(self.day - (7-i)*86400), str(v), '1', '1', '1', '1', '1', 'true']
                for i, v in enumerate(volumes)]

    @patch('gate_market.time.sleep')
    def test_top800_median_not_one_day_spike_and_fixed_on_restart(self, sleep):
        c = self.client()
        names = {f'A{i:04}_USDT' for i in range(800)} | {'SPIKE_USDT', 'ZERO_USDT'}
        def get(path, params):
            self.assertNotIn('limit', params)
            self.assertEqual(params['to'], self.day-1)
            return self.rows([0]*7 if params['currency_pair']=='ZERO_USDT' else
                             [1]*6+[10000000] if params['currency_pair']=='SPIKE_USDT' else [100]*7)
        c._get = Mock(side_effect=get)
        selected = c._select_fixed(names)
        self.assertEqual(len(selected), 800)
        self.assertNotIn('SPIKE_USDT', selected)
        reopened = self.client()
        reopened._get = Mock(side_effect=AssertionError('Must not rescan'))
        self.assertEqual(reopened._select_fixed(names | {'NEW_USDT'}), selected)
        self.assertEqual(reopened._select_fixed(names - {'A0000_USDT'}), selected - {'A0000_USDT'})

    @patch('gate_market.time.sleep')
    def test_failure_resumes_without_publishing_partial_list(self, sleep):
        c = self.client()
        c._get = Mock(side_effect=[self.rows([5]*7), MarketError('network')])
        with self.assertRaises(MarketError):
            c._select_fixed({'A_USDT', 'B_USDT'})
        resumed = self.client()
        resumed._get = Mock(return_value=self.rows([6]*7))
        self.assertEqual(resumed._select_fixed({'A_USDT', 'B_USDT'}), {'A_USDT', 'B_USDT'})
        self.assertEqual(resumed._get.call_count, 1)
        self.assertEqual(resumed._get.call_args.args[1]['currency_pair'], 'B_USDT')

    @patch('gate_market.time.sleep')
    def test_incomplete_week_and_zero_median_excluded(self, sleep):
        c = self.client()
        c._get = Mock(side_effect=[self.rows([5]*6), self.rows([2]*7), self.rows([0]*7)])
        self.assertEqual(c._select_fixed({'A_USDT', 'B_USDT', 'C_USDT'}), {'B_USDT'})

    @patch('gate_market.time.sleep')
    def test_duplicate_day_rejected(self, sleep):
        c = self.client()
        rows = self.rows([4]*7)
        c._get = Mock(return_value=rows + rows[:1])
        with self.assertRaises(MarketError):
            c._select_fixed({'A_USDT'})
