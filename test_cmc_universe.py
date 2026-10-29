import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cmc_universe import fetch_once, validate


def sample():
    return {'version': 1, 'coins': [dict(id=i, name=str(i), symbol='SAME', slug=str(i),
            cmc_rank=i, market_cap_usd=1001-i) for i in range(1, 1001)]}


class CmcTests(unittest.TestCase):
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
