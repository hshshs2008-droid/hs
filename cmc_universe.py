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


class CmcCheckError(ValueError):
    """Only local diagnostic codes, never remote response text or secrets."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CmcCheckError('REDIRECT_REJECTED')


def validate(snapshot, *, require_order=True):
    if not isinstance(snapshot, dict) or snapshot.get('version') != 1:
        raise CmcCheckError('SNAPSHOT_VERSION_INVALID')
    coins = snapshot.get('coins')
    if not isinstance(coins, list) or len(coins) != 1000:
        raise CmcCheckError('COIN_COUNT_' + str(len(coins) if isinstance(coins, list) else 'INVALID'))
    ids = set()
    ranks = set()
    previous = math.inf
    for rank, coin in enumerate(coins, 1):
        if not isinstance(coin, dict) or type(coin.get('id')) is not int or coin['id'] <= 0:
            raise CmcCheckError('IDENTITY_INVALID_AT_' + str(rank))
        if coin['id'] in ids:
            raise CmcCheckError('DUPLICATE_ID_AT_' + str(rank))
        if (type(coin.get('cmc_rank')) is not int or not 1 <= coin['cmc_rank'] <= 1000
                or coin['cmc_rank'] in ranks):
            actual = coin.get('cmc_rank')
            raise CmcCheckError('RANK_INVALID_AT_' + str(rank) + '_ACTUAL_' +
                                (str(actual) if type(actual) is int else 'INVALID'))
        ids.add(coin['id'])
        ranks.add(coin['cmc_rank'])
        for field in ('name', 'symbol', 'slug'):
            if not isinstance(coin.get(field), str) or not coin[field].strip():
                raise CmcCheckError('IDENTITY_METADATA_MISSING_AT_' + str(rank))
        cap = coin.get('market_cap_usd')
        if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0:
            raise CmcCheckError('MARKET_CAP_INVALID_AT_' + str(rank))
        if require_order and cap > previous:
            raise CmcCheckError('MARKET_CAP_ORDER_INVALID_AT_' + str(rank))
        previous = cap
    return snapshot


def normalize(snapshot):
    """Keep CMC's complete rank-1..1000 membership and original ranks.

    Position in that ranked list need not strictly follow numeric market caps.
    Validate identities/rank coverage/values before sorting the stored snapshot.
    """
    validate(snapshot, require_order=False)
    snapshot = dict(snapshot, coins=sorted(snapshot['coins'],
                    key=lambda coin: (-coin['market_cap_usd'], coin['cmc_rank'])))
    return validate(snapshot)


def download(key):
    if not key:
        raise CmcCheckError('CMC_API_KEY_MISSING')
    request = Request(
        'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
        '?start=1&limit=1000&sort=market_cap&sort_dir=desc&convert=USD&cryptocurrency_type=all',
        headers={'Accept': 'application/json', 'X-CMC_PRO_API_KEY': key})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise CmcCheckError('RESPONSE_TOO_LARGE')
    data = json.loads(raw)
    status = data.get('status', {})
    if status.get('error_code') != 0:
        code = status.get('error_code')
        raise CmcCheckError('API_REJECTED_CODE_' + (str(code) if type(code) is int else 'INVALID'))
    coins = []
    for coin in data.get('data', []):
        usd = coin['quote']['USD']
        coins.append({**{k: coin.get(k) for k in ('id', 'name', 'symbol', 'slug', 'cmc_rank', 'platform', 'tags')},
                      'market_cap_usd': usd.get('market_cap'), 'last_updated': usd.get('last_updated')})
    return normalize({'version': 1, 'source': 'CoinMarketCap', 'sort': 'market_cap_desc',
                     'selection': 'cmc_ranks_1_to_1000',
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


def export_market_data(path):
    """Read public exchange identity data for offline review; no CMC credits."""
    from market import BinanceClient
    from gate_market import GateClient
    binance = BinanceClient()
    gate = GateClient(binance, base_url=os.environ.get('API_GATE_BASE') or 'https://api.gateio.ws')
    exchange = binance._get('/api/v3/exchangeInfo', {
        'permissions': 'SPOT', 'showPermissionSets': 'false', 'symbolStatus': 'TRADING'})
    pairs = gate._get('/spot/currency_pairs')
    currencies = gate._get('/spot/currencies')
    if (not isinstance(exchange, dict) or not isinstance(exchange.get('symbols'), list)
            or not isinstance(pairs, list) or not pairs or not isinstance(currencies, list) or not currencies):
        raise CmcCheckError('EXCHANGE_METADATA_INVALID')
    snapshot = {'version': 1, 'binance': exchange['symbols'],
                'gate_pairs': pairs, 'gate_currencies': currencies}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.market-data-', delete=False) as output:
            temporary = output.name
            json.dump(snapshot, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fetch', action='store_true')
    mode.add_argument('--market-data', action='store_true', help='export public exchange metadata for identity review')
    args = parser.parse_args()
    if not args.fetch and not args.market_data:
        parser.print_help()
        return 0
    state = Path(os.environ.get('ALERT_STATE_PATH') or '/data/alerts.sqlite3')
    path = state.parent / 'cmc-top1000.json'
    try:
        if args.market_data:
            destination = state.parent / 'cmc-exchange-metadata.json'
            export_market_data(destination)
            print('MARKET_DATA_OK saved=' + str(destination))
            print('cmc_requests=0 live_scanner_changed=false')
            return 0
        snapshot, cached = fetch_once(path, os.environ.get('CMC_API_KEY', '').strip())
        print('CMC_OK count=1000 order=market_cap_desc cached=' + str(cached).lower())
        print('saved=' + str(path))
        print('fetched_at=' + str(snapshot.get('fetched_at')) + ' credits=' + str(snapshot.get('credit_count')))
        print('live_scanner_changed=false')
        return 0
    except Exception as error:
        # Never print remote bodies, request headers, key, or raw exception text.
        print('CMC_FAILED type=' + type(error).__name__ +
              (' http=' + str(error.code) if isinstance(error, HTTPError) else '') +
              (' reason=' + str(error) if isinstance(error, CmcCheckError) else ''))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
