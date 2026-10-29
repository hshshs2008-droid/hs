import copy
from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cmc_universe import CmcCheckError, export_market_data, fetch_once, main, normalize, validate


def sample():
    return {'version': 1, 'coins': [dict(id=i, name=str(i), symbol='SAME', slug=str(i),
            cmc_rank=i, market_cap_usd=1001-i) for i in range(1, 1001)]}


class CmcTests(unittest.TestCase):
    @patch('gate_market.GateClient')
    @patch('market.BinanceClient')
    def test_public_metadata_export_without_cmc_or_universe_mutation(self, binance, gate):
        binance.return_value._get.return_value = {'symbols': [{'symbol': 'BTCUSDT'}]}
        gate.return_value._get.side_effect = [[{'id': 'ABC_USDT'}], [{'currency': 'ABC', 'name': 'Example'}]]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'metadata.json'
            with patch('cmc_universe.download', side_effect=AssertionError('No CMC requests')):
                result = export_market_data(path)
            self.assertTrue(path.exists())
            self.assertEqual(result['gate_currencies'][0]['name'], 'Example')
        gate.return_value.spot_symbols.assert_not_called()

    def test_rank201_cap_increase_sorted_without_losing_original_rank_or_coin(self):
        original = sample()
        original['coins'][200]['market_cap_usd'] = 1100
        result = normalize(original)
        self.assertEqual(result['coins'][0]['id'], 201)
        self.assertEqual(result['coins'][0]['cmc_rank'], 201)
        self.assertEqual(original['coins'][0]['id'], 1)
        self.assertEqual({c['id'] for c in result['coins']}, set(range(1, 1001)))
        self.assertEqual(validate(result), result)

    def test_normalization_does_not_hide_incomplete_or_duplicate_ranks(self):
        for bad_rank in (200, 1001, None):
            original = sample()
            original['coins'][200]['cmc_rank'] = bad_rank
            with self.assertRaises(CmcCheckError):
                normalize(original)

    @patch('cmc_universe.download')
    def test_normalized_snapshot_can_be_reused_without_refetch(self, download):
        original = sample()
        original['coins'][200]['market_cap_usd'] = 1100
        download.return_value = normalize(original)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'coins.json'
            first, _ = fetch_once(path, 'test')
            again, cached = fetch_once(path, '')
            self.assertTrue(cached)
            self.assertEqual(again, first)
            self.assertEqual(download.call_count, 1)

    def test_cli_exposes_only_controlled_diagnostic_codes(self):
        for error, expected in ((CmcCheckError('CMC_API_KEY_MISSING'), 'reason=CMC_API_KEY_MISSING'),
                                (ValueError('secret_key_value'), 'type=ValueError')):
            output = StringIO()
            with patch('sys.argv', ['cmc_universe.py', '--fetch']), \
                    patch('cmc_universe.fetch_once', side_effect=error), redirect_stdout(output):
                self.assertEqual(main(), 1)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn('secret_key_value', output.getvalue())

    def test_ids_not_tickers_define_unique_assets(self):
        self.assertEqual(len(validate(sample())['coins']), 1000)

    def test_rejects_partial_duplicate_wrong_rank_or_market_cap(self):
        valid = sample()
        variants = [dict(valid, coins=valid['coins'][:-1])]
        for field, value in [('id', 1), ('cmc_rank', 1), ('market_cap_usd', 999999),
                             ('market_cap_usd', float('nan')), ('symbol', '')]:
            item = copy.deepcopy(valid)
            item['coins'][1][field] = value
            variants.append(item)
        for data in variants:
            with self.assertRaises(ValueError):
                validate(data)

    @patch('cmc_universe.download')
    def test_cached_restart_does_not_consume_credits(self, download):
        download.return_value = sample()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'coins.json'
            _, cached = fetch_once(path, 'test')
            self.assertFalse(cached)
            result, cached = fetch_once(path, '')
            self.assertTrue(cached)
            self.assertEqual(len(result['coins']), 1000)
            self.assertEqual(download.call_count, 1)

    @patch('cmc_universe.download', side_effect=ValueError('failed'))
    def test_failure_does_not_publish_partial_list(self, download):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'coins.json'
            with self.assertRaises(ValueError):
                fetch_once(path, 'test')
            self.assertFalse(path.exists())
