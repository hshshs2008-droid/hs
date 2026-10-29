"""Reviewed static CMC universe. No network or automatic refresh."""
import hashlib
import json
from pathlib import Path
import time


def load_universe(path=None):
    from market import EXCLUDED_PAIRS, _valid_symbol
    from gate_market import valid_gate_pair
    raw = Path(path or Path(__file__).with_suffix('.json')).read_bytes()
    data = json.loads(raw)
    if data.get('version') != 1:
        raise ValueError('Invalid approved universe version')
    result = {'Binance': set(), 'Gate.io': set()}
    ids = set()
    for row in data['coins']:
        source, pair, cmc_id = row['source'], row['pair'], row['cmc_id']
        if (type(cmc_id) is not int or cmc_id <= 0 or cmc_id in ids
                or source not in result or pair in result[source] or pair in EXCLUDED_PAIRS
                or not (_valid_symbol(pair) if source == 'Binance' else valid_gate_pair(pair))
                or not pair.endswith('USDT')):
            raise ValueError('Invalid or duplicate approved identity/pair')
        ids.add(cmc_id)
        result[source].add(pair)
    excluded = data['excluded_cmc_ids']
    if (len(result['Binance']) != 322 or len(result['Gate.io']) != 352
            or len(excluded) != 326 or len(set(excluded)) != 326 or ids & set(excluded)):
        raise ValueError('Incomplete approved universe')
    return {k: frozenset(v) for k, v in result.items()}, hashlib.sha256(raw).hexdigest()


def configure_store(store, symbols, version, now_ms=None):
    """Keep history; prevent old pending alerts replaying after source migration."""
    with store._lock, store._db:
        if store._metadata('universe_version', None) != version:
            floor = max(store.strategy_activation_ms,
                        int(time.time() * 1000) if now_ms is None else now_ms)
            for key, value in (('universe_version', version),
                               ('strategy_activation_ms', floor), ('history_reports', {})):
                store._db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)',
                                  (key, json.dumps(value)))
            store.strategy_activation_ms = floor
    store.allowed_symbols = frozenset(symbols)
