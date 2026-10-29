"""Explicit one-time CMC shortlist download; never changes the live scanner.

Run on Railway: python3 cmc_universe.py --fetch
Repeated runs reuse the saved snapshot without consuming API credits.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirect rejected')


def validate(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('version') != 1:
        raise ValueError('Invalid snapshot version')
    coins = snapshot.get('coins')
    if not isinstance(coins, list) or len(coins) != 1000:
        raise ValueError('Expected exactly 1000 coins')
    ids = set()
    previous = math.inf
    for rank, coin in enumerate(coins, 1):
        if not isinstance(coin, dict) or type(coin.get('id')) is not int or coin['id'] <= 0:
            raise ValueError('Invalid CMC identity')
        if coin['id'] in ids or type(coin.get('cmc_rank')) is not int or coin['cmc_rank'] != rank:
            raise ValueError('Duplicate identity or unexpected ranking')
        ids.add(coin['id'])
        for field in ('name', 'symbol', 'slug'):
            if not isinstance(coin.get(field), str) or not coin[field].strip():
                raise ValueError('Missing identity metadata')
        cap = coin.get('market_cap_usd')
        if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0 or cap > previous:
            raise ValueError('Invalid descending market cap')
        previous = cap
    return snapshot


def download(key):
    if not key:
        raise ValueError('Missing CMC_API_KEY')
    request = Request(
        'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
        '?start=1&limit=1000&sort=market_cap&sort_dir=desc&convert=USD&cryptocurrency_type=all',
        headers={'Accept': 'application/json', 'X-CMC_PRO_API_KEY': key})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Oversized CMC response')
    data = json.loads(raw)
    status = data.get('status', {})
    if status.get('error_code') != 0:
        raise ValueError('CMC API rejected request')
    coins = []
    for coin in data.get('data', []):
        usd = coin['quote']['USD']
        coins.append({**{k: coin.get(k) for k in ('id', 'name', 'symbol', 'slug', 'cmc_rank', 'platform', 'tags')},
                      'market_cap_usd': usd.get('market_cap'), 'last_updated': usd.get('last_updated')})
    return validate({'version': 1, 'source': 'CoinMarketCap', 'sort': 'market_cap_desc',
                     'fetched_at': status.get('timestamp'), 'credit_count': status.get('credit_count'),
                     'coins': coins})


def fetch_once(path, key):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return validate(json.loads(path.read_text(encoding='utf-8'))), True
        snapshot = download(key)
        # Publish only a complete validated snapshot, atomically on the volume.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.cmc-', delete=False) as output:
                temporary = output.name
                json.dump(snapshot, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)
        return snapshot, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true')
    args = parser.parse_args()
    if not args.fetch:
        parser.print_help()
        return 0
    state = Path(os.environ.get('ALERT_STATE_PATH') or '/data/alerts.sqlite3')
    path = state.parent / 'cmc-top1000.json'
    try:
        snapshot, cached = fetch_once(path, os.environ.get('CMC_API_KEY', '').strip())
        print('CMC_OK count=1000 order=market_cap_desc cached=' + str(cached).lower())
        print('saved=' + str(path))
        print('fetched_at=' + str(snapshot.get('fetched_at')) + ' credits=' + str(snapshot.get('credit_count')))
        print('live_scanner_changed=false')
        return 0
    except Exception as error:
        # Never print remote bodies, request headers, key, or raw exception text.
        print('CMC_FAILED type=' + type(error).__name__ +
              (' http=' + str(error.code) if isinstance(error, HTTPError) else ''))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
